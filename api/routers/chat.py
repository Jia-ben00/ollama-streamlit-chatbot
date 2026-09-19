"""聊天路由：POST /chat（流式）。

这是整个后端化里最核心、也最容易被问的一块。两个面试必考点：

**1. 流式响应怎么实现？为什么不能直接 return？**

大模型的生成是「逐 token 吐出」的。如果等它全部生成完再一次性 return，
用户要盯着空白页干等好几秒甚至十几秒；而且 Ollama 本身就支持 SSE（流式），
直接把它的流「透传」给前端，用户能立刻看到字一个字一个字地蹦出来。

实现上，FastAPI 用 `StreamingResponse` 包一个生成器：生成器里每次 `yield` 一个
chunk，FastAPI 就推一段给客户端。用 SSE 格式（`data: {...}\n\n`）是为了让浏览器
的 EventSource 能消费。

关键区别：普通端点 `return` 一个 dict → FastAPI 序列化后一次性写回；
流式端点 `return StreamingResponse(gen)` → FastAPI 边生成边写回，响应是
`text/event-stream`，HTTP 连接保持打开直到生成器结束。

**2. 为什么落库和流式是两件事？**

流程是：
  1. 先落一条 user 消息；
  2. 流式调用 Ollama，把每个 chunk 透传给前端，同时在内存里累积完整回复；
  3. 流结束后，落一条 assistant 消息（带 latency_ms）。

不能「边流边落库」：流还没结束，assistant 消息就不完整。所以「持久化」放在
流结束后一次性做；「给用户看」边流边给。

**流式 + 落库的边界情况**：如果流中途断了（客户端断开 / Ollama 报错），
assistant 消息可能没落库——生产上需要补偿（超时后落截断消息），本项目先不做，
保持简单，注释点明。
"""

import json
import time
from typing import Iterator, List, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from api.deps import get_db
from api.schemas import ChatRequest
from cache import cache
from db.models import Conversation, Message, Model
from src.chatbot import DEFAULT_SYSTEM_PROMPT
from src.ollama_client import OllamaClient

router = APIRouter(tags=["chat"])

# 每个会话喂给模型的最大历史条数。
CONTEXT_LIMIT = 20


def _build_context_messages(db: Session, conversation_id: int) -> List[Dict[str, str]]:
    """取会话历史（最近 CONTEXT_LIMIT 条，**不含本次输入**），优先走缓存。

    调用方必须保证在「本次用户消息落库之前」调用本函数 —— 否则读到的历史里
    已经含了本次输入，再追加一次就会让模型看到两遍同一句话。
    """
    cached = cache.get_context(conversation_id)
    if cached is not None:
        # 返回副本：调用方会往这个列表上追加，不复制的话会原地改掉缓存里的对象。
        return list(cached)

    rows = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(CONTEXT_LIMIT)
        .all()
    )
    # DB 里按 id 倒序取的，翻回正序再喂给模型。
    msgs = [
        {"role": m.role, "content": m.content}
        for m in reversed(rows)
    ]
    cache.set_context(conversation_id, msgs)
    return msgs


@router.post("/chat")
def chat_stream(
    payload: ChatRequest,
    db: Session = Depends(get_db),
):
    """流式聊天：透传 Ollama 的流，结束后落库。"""
    conversation_id = payload.conversation_id
    user_content = payload.content

    # 校验会话存在；同时显式取 model 名，避免懒加载在生成器里二次查库。
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 会话绑定的模型名（会话创建时指定的 models.name）。
    model_row = (
        db.query(Model.name)
        .join(Conversation, Conversation.model_id == Model.id)
        .filter(Conversation.id == conversation_id)
        .first()
    )
    model_name = model_row[0] if model_row else None

    # 1. 先取历史上下文。
    #    ⚠️ 这一步**必须在落库之前**。落库之后再查，查出来的最近 N 条里已经包含
    #    本次输入，下面再 append 一次，模型就会看到两遍同一句话
    #    （CONTEXT_LIMIT=20 实际只剩 10 句有效历史）。
    history = _build_context_messages(db, conversation_id)

    # 2. 落用户消息。
    user_msg = Message(
        conversation_id=conversation_id,
        role="user",
        content=user_content,
    )
    db.add(user_msg)
    db.commit()

    # 3. 组装本次请求的消息（历史 + 本次输入），交给 Ollama 流式生成。
    request_messages = history + [{"role": "user", "content": user_content}]

    ollama = OllamaClient()

    def generate() -> Iterator[str]:
        full_reply: List[str] = []
        started = time.perf_counter()

        try:
            for chunk in ollama.chat_stream(
                messages=[{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}] + request_messages,
                model=model_name,
            ):
                full_reply.append(chunk)
                yield f"data: {json.dumps({'chunk': chunk}, ensure_ascii=False)}\n\n"
        except ConnectionError as exc:
            yield f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"
            return

        latency_ms = int((time.perf_counter() - started) * 1000)
        reply = "".join(full_reply)

        # 3. 流结束后落 assistant 消息（带 latency_ms）。
        assistant_msg = Message(
            conversation_id=conversation_id,
            role="assistant",
            content=reply,
            latency_ms=latency_ms,
        )
        db.add(assistant_msg)
        db.commit()

        # 写穿缓存：把本轮新增的「用户输入 + 模型回复」并入上下文后写回。
        #
        # 为什么不是 invalidate（删除）？删掉之后，下一次请求进来时缓存必然是空的，
        # 于是每次都回 DB 重建 —— 读了缓存却永远 MISS，命中率恒为 0，
        # Redis 这一层在功能上等于没接（且「写后立刻删」，setex 白做一次）。
        #
        # 为什么不是只 set 本轮两条？那会把更早的对话丢掉，命中缓存的请求只能
        # 看到最近两句。所以要用「本轮用到的完整上下文 + 本轮回复」重建，再截断。
        new_context = (
            request_messages + [{"role": "assistant", "content": reply}]
        )[-CONTEXT_LIMIT:]
        cache.set_context(conversation_id, new_context)

        yield f"data: {json.dumps({'done': True, 'latency_ms': latency_ms}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
