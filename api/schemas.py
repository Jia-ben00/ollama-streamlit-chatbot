"""Pydantic 请求/响应模型（schemas）。

这层是「HTTP 边界」的契约：外部进来的请求长什么样、返回给客户端的数据长什么样，
都在这里用 Pydantic 模型声明，和内部的 ORM 模型（db/models.py）分离。

为什么要分离（面试会问）：
- ORM 模型（如 Message）和 API 模型（如 MessageOut）职责不同。ORM 是「数据库的
  形状」，API 是「接口的形状」。把 Message ORM 对象直接返回给前端，会泄漏
  内部字段（比如将来加了个 `deleted_at` 软删字段不该暴露），也把「改数据库」
  和「改接口」耦合在一起。
- Pydantic 自带校验：请求进来先过 schema，字段类型不对、缺必填、枚举越界，都会
  在进入业务逻辑前被拦下，返回 422，而不是让错误一路传到 DB 层才炸。

字段设计原则：请求模型只收「业务需要的最小集合」，响应模型显式列出「允许外泄
的字段」。这里用 `model_config = ConfigDict(from_attributes=True)` 让 Pydantic
能直接从 ORM 对象提取字段，省去手写 .dict() 转换。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── 会话 ──────────────────────────────────────────────
class ConversationCreate(BaseModel):
    """创建会话的请求体。"""

    title: str = Field(..., min_length=1, max_length=200)
    model_id: int
    user_id: int


class ConversationOut(BaseModel):
    """会话响应。附 message_count，避免前端为列表逐个查消息数（N+1）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    model_id: int
    title: str
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    message_count: int = 0  # 聚合得来，不是 ORM 字段


class ConversationUpdate(BaseModel):
    """PATCH /conversations/{id} 的请求体：**局部更新**。

    用 `Optional` + 默认 `None` 表达「本次不改这个字段」，而不是要求客户端
    把整个会话对象发回来（PUT 的语义）。这就是 PATCH 与 PUT 的区别：
    - PUT 是「用我给的完整对象替换你的」——客户端必须知道所有字段的当前值；
    - PATCH 是「只改我说的字段」——客户端不知道的字段自然不会被误覆盖。

    为什么这对前端很关键：前端切换模型时只知道 `model_id` 变了，并不知道
    `title` 当前是什么。如果用 PUT，前端就得先 GET 一次再全量回填，
    多一次往返，还可能在「读—改—写」之间覆盖掉别人的修改（丢失更新）。
    """

    title: Optional[str] = Field(None, min_length=1, max_length=200)
    model_id: Optional[int] = None
    is_archived: Optional[bool] = None


# ── 消息 ──────────────────────────────────────────────
class MessageCreate(BaseModel):
    """发送消息的请求体（用户消息）。"""

    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str = Field(..., min_length=1)
    token_count: int = 0
    latency_ms: Optional[int] = None


class MessageOut(BaseModel):
    """消息响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    role: str
    content: str
    token_count: int
    latency_ms: Optional[int]
    created_at: datetime


# ── 聊天 ──────────────────────────────────────────────
class ChatRequest(BaseModel):
    """POST /chat 的请求体。

    让客户端指定会话，便于后端把消息落库到正确的 conversation。
    """

    conversation_id: int
    content: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    """POST /chat 的响应。"""

    conversation_id: int
    reply: str
    model: str
    latency_ms: int


# ── 目录（catalog）：模型与用户 ────────────────────────
# 这两个接口是「前端接入后端」时才暴露出来的缺口：前端要建会话就必须知道
# user_id 和 model_id，而这两个 id 来自数据库而不是前端硬编码。
#
# 为什么必须有它们：如果前端把 `user_id=1, model_id=1` 写死，那么库里的
# 自增 id 一变（换台机器导入数据、重建库），前端就指向了错误的模型。
# 这是「前端不应该知道数据库主键」这个原则的具体落地——但演示项目没有鉴权，
# 所以退而求其次：让服务端告诉前端「有哪些可选」，而不是让前端猜。
class ModelOut(BaseModel):
    """模型响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    provider: str
    param_size: str
    context_window: int
    is_active: bool


class UserOut(BaseModel):
    """用户响应。

    注意这里**故意不含 email**：即便 `users` 表里有这一列，也不代表它该出现在
    接口响应里。邮箱是 PII（个人身份信息），一个「给前端选当前用户」的下拉框
    不需要它。Pydantic 的响应模型在这里起的是「白名单」作用——声明了才外泄，
    没声明的字段（哪怕 ORM 对象上有）一律不出去。
    这就是「为什么不要直接把 ORM 对象 return 出去」最直观的例子。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    plan: str
