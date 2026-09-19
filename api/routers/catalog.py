"""目录路由：`GET /models` 与 `GET /users`。

这两个接口是「把前端真正接上后端」时才浮现出来的缺口，值得单独说清楚——
因为它们是「先有后端、后有前端」的典型症状：

后端骨架先做完的时候，接口是按「已经知道 user_id / model_id 的调用方」设计的：
`POST /conversations` 要 `user_id` 和 `model_id`，`POST /chat` 要 `conversation_id`。
这些 id 从哪来？后端当初的答案是「调用方自己知道」——于是 e2e 脚本里写的就是
`user_id=1`、`model_id=1`。

但真实的 Streamlit 前端并不知道这些 id：它连数据库都连不上（这正是后端化的目的）。
所以前端接进来的第一个动作必然是「问服务端有哪些模型、哪些用户可选」。
**接口设计漏掉的从来不是功能，而是「调用方拿到入口的那一步」。**

顺带一个设计细节：为什么不合并成一个 `GET /bootstrap`？
因为两个资源的缓存特性、权限要求、变更频率都不一样（模型列表几乎不变、用户列表
归鉴权管），合并接口能省一次往返，但会把「将来给用户接口加鉴权」变成「给
bootstrap 加鉴权」，牵连到模型列表。**一次往返的优化，不值得把两个生命周期
不同的资源绑在一起。**
"""

from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.deps import get_db
from api.schemas import ModelOut, UserOut
from db.models import Model, User

router = APIRouter(tags=["catalog"])


@router.get("/models", response_model=List[ModelOut])
def list_models(
    active_only: bool = Query(True, description="只返回 is_active=1 的模型"),
    db: Session = Depends(get_db),
):
    """列出可用的模型。

    `active_only` 默认 True：`models` 表里的 `is_active` 是「这个模型是否还在
    服务」的开关（比如某个模型下线了、或者授权到期了），前端选择器不应该把
    已下线的模型列出来让用户选。但**接口层保留参数**而不是在 SQL 里写死条件——
    因为管理端/排查场景需要看到全部（含已下线的），这是「同一份数据、不同调用方
    需要不同视图」的正常情况，用参数区分比开两个接口干净。

    `provider` 一起返回，是因为这个库的模型不只有 Ollama（还有 openai /
    anthropic / zhipu）。前端未来要按 provider 分组展示，现在先把字段带上，
    免得以后加。
    """
    q = db.query(Model)
    if active_only:
        q = q.filter(Model.is_active.is_(True))
    return q.order_by(Model.id).all()


@router.get("/users", response_model=List[UserOut])
def list_users(db: Session = Depends(get_db)):
    """列出用户（演示用：给前端一个「当前用户」选择器）。

    真实的系统里**不会有这个接口**——「我是谁」应该由请求里的凭据（Cookie /
    JWT / Session）解出来，而不是让客户端从用户列表里自己挑一个。列出全部用户
    本身就是越权：一个用户不该看见别的用户。

    这里保留它，因为后端化改造的范围是「HTTP 层 + 数据层」，鉴权不在本次范围内，
    而前端确实需要一个 user_id 才能建会话。**所以这是一个明确标注的技术债**，
    而不是设计疏漏：代码里写清楚它为什么存在、真实做法是什么，
    比假装它是正确的设计更诚实。

    响应模型 `UserOut` 里没有 `email`——即便这张表有这一列。
    """
    return db.query(User).order_by(User.id).all()
