"""API 依赖注入层（deps.py）。

把「从请求里拿资源」的逻辑集中在这里，路由里只写业务。目前只有 DB session，
Redis 缓存用 cache 模块的全局单例（见 api/main.py 里的 lifespan 说明）。
"""

from typing import Generator

from sqlalchemy.orm import Session

from db.session import get_db

# get_db 直接复用 db.session 里的实现，避免两处维护。
# 单独建 deps.py 是给未来「更多依赖」留位置（比如当前用户注入、鉴权、限流），
# 也让 import 路径更清晰：路由只 from api.deps import get_db。
__all__ = ["get_db"]

# 类型注解帮助 IDE 提示，运行时无副作用。
DatabaseSession = Session


def db_session() -> Generator[Session, None, None]:
    """FastAPI 依赖别名，语义上等价于 get_db。"""
    yield from get_db()
