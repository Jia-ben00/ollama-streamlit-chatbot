"""数据库 ORM 模型层。

这一层是把「练习库 chatbot」里那 6 张表，用 SQLAlchemy 的 declarative 风格
一一映射成 Python 类。之所以要用 ORM 而不是手写 SQL，三个原因（面试会问）：

1. **类型与迁移**：字段约束（NOT NULL / 唯一 / 外键 / 枚举）在定义时就写死，
   建表脚本 `init_db.py` 用 `Base.metadata.create_all()` 一键生成，不用手工维护
   两份 schema（一份 DDL、一份模型）。

2. **关系导航**：`Conversation.messages` / `User.conversations` 这种对象关系，
   比手写 JOIN 更容易读、更不容易写错别名。但注意——关系属性只是「便利」，
   真到查询热点（比如会话列表）还是得显式 JOIN + 聚合，避免 N+1（见 api/routers/conversations.py）。

3. **与练习库同一套 schema**：这里每张表的字段、类型、索引、外键，都严格对应
   `SHOW CREATE TABLE chatbot.*` 的真实结构，不是我自己臆想的。学 SQL 的 8 小时
   有一半直接变成了这份代码。

字段类型为什么这么选（对照真实表）：
- 主键 `id`：练习库里是 `int unsigned`（users/models/tags/conversation_tags 的 tag_id）
  或 `bigint unsigned`（conversations/messages）。ORM 用 MySQL 方言的
  `INTEGER(unsigned=True)` / `BIGINT(unsigned=True)` 对齐——注意 `unsigned` 是
  MySQL 方言参数，不是 SQLAlchemy 通用参数（写成 `Integer(unsigned=True)` 会直接
  报 TypeError，这是「通用类型 vs 方言类型」的典型区别）。
- `tinyint(1)` 的布尔（is_archived / is_active）：MySQL 里 tinyint(1) 就是布尔，
   ORM 用 `Boolean` 映射，读出来是 True/False 而不是 0/1。
- `enum('system','user','assistant')` / `enum('free','pro')`：用 SQLAlchemy 的
   `Enum`，并在 MySQL 上让它生成原生 ENUM 类型（`native_enum=True`），保证和
   练习库 `SHOW CREATE TABLE` 里的 enum 完全一致，而不是退化成 VARCHAR。
- `datetime` + `CURRENT_TIMESTAMP` / `on update CURRENT_TIMESTAMP`：
   用 `server_default=func.now()` + `onupdate=func.now()`，让时间戳由数据库端维护，
   而不是应用端 `datetime.now()`。这样多个应用实例写同一张表时时间是一致的，
   也不会因为应用时钟漂移产生脏数据。
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.mysql import BIGINT, INTEGER
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _table_args() -> dict:
    """每张表都要带的方言参数：显式声明字符集与排序规则。

    为什么必须写死在模型里，而不是交给「数据库默认值」：
    不声明时，`CREATE TABLE` 的表字符集**继承数据库的默认值**。本机练习库恰好是
    utf8mb4_0900_ai_ci，所以一直没出问题；但同一份代码换到默认字符集是 latin1 的
    实例上（老 my.cnf、老镜像），建出来的表就是 latin1，INSERT emoji 会直接报
    `Incorrect string value: '\\xF0\\x9F...'`。
    也就是说：不写的话，「能不能存 4 字节字符」这件事被寄托在服务器配置上；
    写进模型之后，它才变成代码的一部分。

    MySQL 的 `utf8` 只有 3 字节，存不下 emoji —— 这就是为什么这里必须是 `utf8mb4`，
    也是面试第 6 问的答案。

    用函数返回新 dict，而不是定义一个模块级常量被 6 张表共用：
    避免多张表引用同一个可变对象（SQLAlchemy 处理 __table_args__ 时会读取这个 dict）。
    """
    return {
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_0900_ai_ci",
    }


class User(Base):
    """用户表，对应 chatbot.users。

    唯一约束有两个：username 和 email 都 UNIQUE（uk_users_username / uk_users_email）。
    登录/注册时靠这两个唯一键做幂等——重名重邮箱会直接抛 IntegrityError，
    在 api 层捕获后转成 409，而不是先查再插（查再插有竞态窗口）。
    """

    __tablename__ = "users"
    __table_args__ = _table_args()

    id = Column(INTEGER(unsigned=True), primary_key=True, autoincrement=True)
    username = Column(String(50), nullable=False, unique=True)
    email = Column(String(120), nullable=False, unique=True)
    # enum('free','pro')，默认 free。映射成 Python 字符串，比 int 更可读。
    plan = Column(Enum("free", "pro", native_enum=True), nullable=False, server_default="free")
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    conversations = relationship("Conversation", back_populates="user")


class Model(Base):
    """模型表，对应 chatbot.models。

    注意这里的 `provider` 字段——练习库里不止 Ollama，还有 openai / anthropic /
    zhipu。所以这张表是「模型注册表」，`name` 唯一。`param_size` 是 varchar(10)，
    存 "7B" / "14B" 这种字符串（不是数值，因为还有 "-" 这种无法量化的），
    所以 ORM 里也是 String 而不是 Float。
    """

    __tablename__ = "models"
    __table_args__ = _table_args()

    id = Column(INTEGER(unsigned=True), primary_key=True, autoincrement=True)
    name = Column(String(60), nullable=False, unique=True)
    provider = Column(String(30), nullable=False)
    param_size = Column(String(10), nullable=False)
    context_window = Column(INTEGER(unsigned=True), nullable=False)
    is_active = Column(Boolean, nullable=False, server_default="1")

    conversations = relationship("Conversation", back_populates="model")


class Conversation(Base):
    """会话表，对应 chatbot.conversations。

    两个外键：user_id -> users.id，model_id -> models.id，各带独立索引
    （idx_conv_user / idx_conv_model）。这两个索引是「按用户列会话」「按模型列会话」
    的查询基础——没有它们，这两类查询都会退化成全表扫描。

    is_archived 是「软归档」而不是删除：会话留着历史，只是从默认列表里隐藏。
    这对应真实产品里「删除会话」通常是软删，方便用户找回 / 便于审计。
    """

    __tablename__ = "conversations"
    __table_args__ = _table_args()

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    user_id = Column(
        INTEGER(unsigned=True), ForeignKey("users.id"), nullable=False, index=True
    )
    model_id = Column(
        INTEGER(unsigned=True), ForeignKey("models.id"), nullable=False, index=True
    )
    title = Column(String(200), nullable=False)
    is_archived = Column(Boolean, nullable=False, server_default="0")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    # on update CURRENT_TIMESTAMP：每次 UPDATE 这行，时间戳自动刷新。
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user = relationship("User", back_populates="conversations")
    model = relationship("Model", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation")
    # 多对多：会话 <-> 标签，通过 conversation_tags 中间表。
    tags = relationship("Tag", secondary="conversation_tags", back_populates="conversations")


class Message(Base):
    """消息表，对应 chatbot.messages。

    三个关键设计点（面试高频）：
    1. `role` 是 enum('system','user','assistant')，每条消息限定三种角色之一。
       system 消息是「会话级系统提示」，一个会话通常只有一条 system 打头。
    2. `token_count` / `latency_ms` 是**用量与性能观测**字段——前者记账，
       后者记录 Ollama 每次生成耗时。这两个字段让这个项目不是「玩具聊天」，
       而是「带可观测性的后端」，这是后端化的核心价值点之一。
    3. `conversation_id` 带索引 idx_msg_conv，且 `created_at` 带索引 idx_msg_created。
       但——idx_msg_created 在真实数据上**并不总能被用上**（见 init_db.py 顶部
       关于 EXPLAIN 的说明），这正是「索引不等于一定生效」的活教材。
    """

    __tablename__ = "messages"
    __table_args__ = _table_args()

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    conversation_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("conversations.id"),
        nullable=False,
        index=True,
    )
    role = Column(
        Enum("system", "user", "assistant", native_enum=True), nullable=False
    )
    content = Column(Text, nullable=False)
    token_count = Column(INTEGER(unsigned=True), nullable=False, server_default="0")
    # latency_ms 可空：只有 assistant 消息才有生成耗时，user/system 是 NULL。
    latency_ms = Column(INTEGER(unsigned=True), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)

    conversation = relationship("Conversation", back_populates="messages")


class Tag(Base):
    """标签表，对应 chatbot.tags。name 唯一（uk_tags_name）。"""

    __tablename__ = "tags"
    __table_args__ = _table_args()

    id = Column(INTEGER(unsigned=True), primary_key=True, autoincrement=True)
    name = Column(String(30), nullable=False, unique=True)

    conversations = relationship("Conversation", secondary="conversation_tags", back_populates="tags")


class ConversationTag(Base):
    """会话-标签关联表，对应 chatbot.conversation_tags。

    纯关联表（多对多中间表）。主键是 (conversation_id, tag_id) 复合主键，
    天然保证「同一会话同一标签只出现一次」；另外给 tag_id 单列建了 idx_ct_tag，
    支持「按标签反查会话」。
    """

    __tablename__ = "conversation_tags"
    __table_args__ = _table_args()

    conversation_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("conversations.id"),
        primary_key=True,
    )
    tag_id = Column(
        INTEGER(unsigned=True), ForeignKey("tags.id"), primary_key=True, index=True
    )


# 补充：messages 的 created_at 索引在真实数据上不生效的原因，以及为什么这里不删。
#
# 用本地库实测（EXPLAIN SELECT * FROM messages WHERE created_at >= '2026-01-01'
# ORDER BY created_at DESC）：
#   type=ALL, key=NULL, rows=3857, Extra="Using where; Using filesort"
# 也就是说，MySQL 优化器判定「全表 + 内存排序」比「走 idx_msg_created 再回表」更便宜。
# 原因：3857 行里几乎所有行都满足 created_at 范围（数据时间跨度集中），索引选择性
# 几乎为 0，走索引反而多一次回表。这就是「索引存在但没被用」的典型场景——
# 面试官问「为什么建了时间索引还全表扫」，答案是「选择性 + 回表成本」。
#
# 为什么代码里仍保留这个索引：真实线上数据量上去后（几十万行、时间跨度拉大），
# 范围查询的选择性会变高，索引会重新生效。练手库里 3.8k 行不足以让它生效，
# 不代表它不该存在。这正是「本地跑通 ≠ 生产成立」的另一个维度。
