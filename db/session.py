"""数据库引擎与连接池管理。

这一层只干一件事：把「怎么连数据库、连接池多大、多久回收」这些配置集中起来，
对外暴露一个 `get_db()` 依赖（FastAPI 用），和一份 `DATABASE_URL`。

为什么需要连接池（面试必问）：
MySQL 每建立一个连接都要走 TCP 握手 + 认证 + 线程分配，成本很高（几十毫秒到上百毫秒）。
如果每个 HTTP 请求都新建一个连接，高并发下数据库会被连接风暴打垮。
连接池就是「预开一批连接、用完归还、复用」，把建连成本摊薄到近乎为零。

`pool_size` 和 `max_overflow` 怎么定（面试必问）：
- pool_size = 5：常驻连接数。这个项目是「个人后端 + 学习载体」，并发极低，
  5 个常驻连接绰绰有余。连接池不是越大越好——每个常驻连接都会占用 MySQL
  一个线程（threads_connected），开 100 个空转连接反而浪费服务器资源。
- max_overflow = 10：突发峰值时允许在 5 个之上再临时开到 15 个，峰值过后回收。
  这样平时不占资源，突发不被打挂。
- 经验公式：pool_size 略大于「稳态并发请求数」，overflow 兜住峰值。
  面试官如果问「怎么知道该设多大」，答案是「压测 + 监控 threads_connected」。

`pool_recycle` 为什么是 3600 秒（面试必问）：
MySQL 默认的 wait_timeout（8 小时）会在连接空闲太久后主动断开。如果连接池里
有连接空闲超过 8 小时，应用下次拿它来用时才发现「连接已被服务器关闭」，报
`MySQL server has gone away`。pool_recycle=3600 让 SQLAlchemy 每小时主动丢弃并
重建空闲连接，永远在服务器断开之前自己先换掉，避免偶发「第一次请求报错」。

`pool_pre_ping`：每次从池里拿连接时先 ping 一下确认还活着，死了就换一条。
这是对「gone away」的第二道保险，代价是一次极轻的 ping，值得开。
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 连接串从环境变量读。默认不写死密码——本机开发时通过 .env 提供
# （.env 已在 .gitignore 里，不会进仓库），容器里通过 docker-compose 注入。
#
# 为什么不让默认值带真实密码：代码文件会进 git 历史、进公开仓库，
# 把凭据写死在源码里是「凭据泄漏」的高危反模式。正确姿势是「代码只管读，
# 凭据从外部注入」（环境变量 / 密钥管理 / .env）。
#
# charset 用 utf8mb4 而不是 utf8（面试必问：为什么是 utf8mb4？）：
# MySQL 的 utf8 其实是「阉割版」UTF-8，每个字符最多 3 字节，存不下 emoji 和
# 部分生僻字（4 字节字符）。utf8mb4 才是真正的 UTF-8（最多 4 字节）。
# 用户聊天内容里 emoji 是常态（😀🔥 都是 4 字节），用 utf8 会直接插不进去报
# "Incorrect string value"。练习库 6 张表全是 utf8mb4_0900_ai_ci，这里对齐。
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://root@127.0.0.1:3306/chatbot?charset=utf8mb4",
)

engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_recycle=3600,
    pool_pre_ping=True,
    # 打印 SQL 便于调试；生产环境应该用日志框架而不是 echo。
    echo=False,
)

# 会话工厂：每个请求拿一个独立 Session，用完归还连接。
# expire_on_commit=False 让 commit 后对象属性仍可读，避免序列化时报
# "Instance is detached"。
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI 依赖：为每个请求提供一个数据库会话，请求结束自动关闭。

    用法：在路由函数参数里写 `db: Session = Depends(get_db)`。
    这里用 yield 而不是 return，是为了把「关闭会话」放在请求结束后的
    finally 里执行——即使请求中途抛异常，连接也会被正确归还，不会泄漏。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
