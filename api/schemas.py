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
