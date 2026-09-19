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

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.deps import get_db
from api.schemas import (
    ConversationCreate,
    ConversationOut,
    ConversationUpdate,
    MessageOut,
)
from cache import cache
from db.models import Conversation, Message, Model

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


@router.get("/{conversation_id}/messages", response_model=List[MessageOut])
def list_messages(
    conversation_id: int,
    limit: int = Query(50, ge=1, le=200),
    before_id: Optional[int] = Query(None, description="只取 id 小于它的消息（向前翻页游标）"),
    db: Session = Depends(get_db),
):
    """拉取会话消息列表（默认最近 50 条，按时间正序返回）。

    分页为什么用 `before_id` 游标而不是 `OFFSET`（面试加分点）：
    - `LIMIT 50 OFFSET 500` 的语义是「先扫出前 550 行、丢掉前 500 行」。翻到越后面，
      扫过的行越多，性能随页深线性退化，而且聊天场景是「一直往下滚」，页深没有上限。
    - 游标分页（`WHERE id < :before_id ORDER BY id DESC LIMIT 50`）每次都从索引上
      直接定位到起点，只读它需要的那 50 行，**任意页深成本恒定**。
    - 附带好处：游标是「最后一条的 id」，用户往上滚时新消息插进来不会导致错位
      （OFFSET 分页在有新数据写入时会漏行/重复行）。

    返回前把倒序结果翻回正序，前端拿到就能直接按时间渲染，不用自己 reverse。
    """
    # 会话不存在时返回 404，而不是空列表——空列表会让前端误以为「会话存在但没消息」。
    exists = db.query(Conversation.id).filter(Conversation.id == conversation_id).first()
    if exists is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    q = db.query(Message).filter(Message.conversation_id == conversation_id)
    if before_id is not None:
        q = q.filter(Message.id < before_id)

    rows = q.order_by(Message.id.desc()).limit(limit).all()
    return list(reversed(rows))


@router.delete("/{conversation_id}/messages")
def clear_messages(
    conversation_id: int,
    db: Session = Depends(get_db),
):
    """清空会话的全部消息（会话本身保留，`messages` 表里对应行真删除）。

    为什么是「清空消息」而不是「删除会话」：前端上那颗按钮的语义是「清空对话」，
    用户想保留这个会话对象（标题、所属模型、标签都还在），只是把聊天记录抹掉。
    如果做成删会话，用户回来发现会话没了，还得重新建一个——语义不匹配。

    **这个接口里最关键的一行是 `cache.invalidate()`。**
    缓存里存着「会话最近 20 条消息 = 喂给模型的上下文」。如果只删库不清缓存，
    下一次提问时后端会把缓存里那些**已经被用户删掉的消息**重新拼进 prompt 发给
    模型——模型会接着一段用户认为不存在的对话往下聊。这类 bug 极难排查，
    因为接口全部返回成功、数据库里也确实没有那些行。

    这就是「缓存失效」为什么必须和「写操作」绑在一起：**任何改变数据的路径，
    都要问一句「我动了的那份数据，在缓存里有没有副本」**。只靠 TTL 兜底是不够的，
    TTL 只保证「最终一致」，中间这段时间用户看到的是错的。

    但要分清：**「失效」不等于「一律删除」**。这里用 `invalidate()`，是因为消息被
    清空之后，缓存里那份上下文**整体作废**；而**常规对话**是「追加」语义，正确做法是
    写穿（`set_context` 把新消息并回去），那里若也改成删除，就会变成下轮开头必然
    MISS、命中率恒为 0 —— 也就是 `cache.py` 模块注释里记的那个坑。
    """
    exists = db.query(Conversation.id).filter(Conversation.id == conversation_id).first()
    if exists is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    # synchronize_session=False：直接下发 DELETE，不把 session 里已加载的对象逐个
    # 同步（这里本来也没加载过 Message 对象），省一轮开销。
    deleted = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .delete(synchronize_session=False)
    )
    db.commit()

    # 先删库、再失效缓存。顺序不能反：反过来的话，万一 commit 失败，
    # 缓存已经没了、库里还在，虽然结果一致（下次查询回源重建），但白丢一次缓存；
    # 而在「删库成功、失效失败」的情况下，缓存虽然脏着，TTL 也会兜底。
    # 更要紧的是：缓存失效放在 commit 之后，才不会把「未提交的数据」当成已生效。
    cache.invalidate(conversation_id)

    return {"conversation_id": conversation_id, "deleted": deleted}


@router.patch("/{conversation_id}", response_model=ConversationOut)
def update_conversation(
    conversation_id: int,
    payload: Optional[ConversationUpdate] = None,
    db: Session = Depends(get_db),
):
    """局部更新会话：改标题 / 换模型 / 归档。

    **为什么换模型要走这里**：`POST /chat` 的模型不是请求参数，而是「会话创建时
    绑定在 conversations.model_id 上的」。这样设计的好处是「一个会话的模型是稳定的」
    ——同一段对话不会因为前半段用 A 模型、后半段用 B 模型而变得上下文不连贯。
    代价就是「用户想换模型」必须有地方改这个绑定，也就是这个 PATCH。

    **兼容说明（真实的接口演进）**：不传 body 时按「归档」处理。这是第一版接口的
    行为——当时 PATCH 只用来归档，语义完全隐式（光看路径看不出会归档），
    `tests/e2e/smoke.py` 也依赖它。第二版既要支持换模型，又不能一脚踢翻老调用方，
    所以选择「加可选字段 + 保留旧默认路径」，而不是另开一个 POST /archive
    （那会让「同一个资源的不同字段更新」散落到多个端点，更乱）。
    新代码请显式传 `{"is_archived": true}`，别依赖这个默认。
    """
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    if payload is None:
        # 旧行为：无 body 即归档。
        conv.is_archived = True
    else:
        # 只改显式传了的字段——这就是 PATCH 与 PUT 的区别：
        # 客户端不必知道 title 当前是什么，也就不会把别人的修改覆盖掉。
        if payload.title is not None:
            conv.title = payload.title
        if payload.model_id is not None:
            # 显式校验外键目标存在。不查的话，非法 model_id 会在 commit 时撞外键约束，
            # 冒出一个 500 IntegrityError；调用方拿到 500 只会以为是服务炸了。
            # 在边界层拦下并返回 400，错误语义才是准的：
            # 「外键约束」保证的是**数据一致性**，它不负责**错误语义**。
            model_exists = (
                db.query(Model.id).filter(Model.id == payload.model_id).first()
            )
            if model_exists is None:
                raise HTTPException(status_code=400, detail="模型不存在")
            conv.model_id = payload.model_id
        if payload.is_archived is not None:
            conv.is_archived = payload.is_archived

    db.commit()
    db.refresh(conv)

    count = (
        db.query(func.count(Message.id))
        .filter(Message.conversation_id == conversation_id)
        .scalar()
    )
    out = ConversationOut.model_validate(conv)
    out.message_count = count or 0
    return out
