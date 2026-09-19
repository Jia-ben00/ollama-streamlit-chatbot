"""会话 CRUD 路由。

这里的核心知识点是「会话列表为什么特殊处理」（面试必问）。

naive 做法（会触发 N+1）：
    conversations = db.query(Conversation).filter(...).all()
    for c in conversations:
        c.message_count = len(c.messages)   # 每次访问 c.messages 都发一条 SQL

上面这种写法，300 个会话就是 1（查会话）+ 300（逐个查消息）= 301 条 SQL。
这就是经典的 N+1 查询问题——列表越长，查询次数线性膨胀。

正确做法（单条 SQL 完成）：
用 LEFT JOIN + GROUP BY + COUNT 一次把所有会话连同各自消息数一起取出来：
    SELECT c.*, COUNT(m.id) AS message_count
    FROM conversations c
    LEFT JOIN messages m ON m.conversation_id = c.id
    GROUP BY c.id

本地库实测 EXPLAIN 也印证了这一点：按 user_id 过滤会话走 idx_conv_user（ref），
消息计数走 idx_msg_conv（covering index, Using index），是索引友好的执行计划。

本模块用 SQLAlchemy 的显式 join + func.count 表达同样的查询，而不是靠 relationship
懒加载——这就是「ORM 便利性」和「查询性能」之间的取舍：热路径上，显式 JOIN 优于
relationship 懒加载。
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.deps import get_db
from api.schemas import ConversationCreate, ConversationOut
from db.models import Conversation, Message

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=List[ConversationOut])
def list_conversations(
    user_id: int,
    db: Session = Depends(get_db),
):
    """列出某用户的会话（含消息数），单条 SQL 避免 N+1。"""
    rows = (
        db.query(
            Conversation,
            func.count(Message.id).label("message_count"),
        )
        .outerjoin(Message, Message.conversation_id == Conversation.id)
        .filter(Conversation.user_id == user_id)
        .group_by(Conversation.id)
        .order_by(Conversation.updated_at.desc())
        .all()
    )

    result = []
    for conv, count in rows:
        item = ConversationOut.model_validate(conv)
        item.message_count = count
        result.append(item)
    return result


@router.post("", response_model=ConversationOut, status_code=201)
def create_conversation(
    payload: ConversationCreate,
    db: Session = Depends(get_db),
):
    """创建会话。"""
    conv = Conversation(
        user_id=payload.user_id,
        model_id=payload.model_id,
        title=payload.title,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)

    out = ConversationOut.model_validate(conv)
    out.message_count = 0
    return out


@router.get("/{conversation_id}", response_model=ConversationOut)
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
):
    """获取单个会话及其消息数。"""
    row = (
        db.query(Conversation, func.count(Message.id).label("message_count"))
        .outerjoin(Message, Message.conversation_id == Conversation.id)
        .filter(Conversation.id == conversation_id)
        .group_by(Conversation.id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    conv, count = row
    out = ConversationOut.model_validate(conv)
    out.message_count = count
    return out


@router.patch("/{conversation_id}", response_model=ConversationOut)
def archive_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
):
    """归档会话（软删除）。"""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    conv.is_archived = True
    db.commit()
    db.refresh(conv)

    count = db.query(func.count(Message.id)).filter(Message.conversation_id == conversation_id).scalar()
    out = ConversationOut.model_validate(conv)
    out.message_count = count or 0
    return out
