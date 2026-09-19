"""FastAPI 应用入口。

lifespan（启动/关闭钩子）是这里的关键，面试会问「连接池什么时候建、什么时候释放」。

为什么用 lifespan 而不是模块级直接建连接池？
- 模块级 `engine = create_engine(...)` 会在 import 时就建连接（惰性的，实际首查才连），
  而 lifespan 让「建连」和「应用生命周期」绑定：服务启动时建池、关停时释放池。
- 对 DB 引擎，SQLAlchemy 的连接池本身是惰性的（用到才连），所以这里 lifespan 里
  对 DB 主要是「预热 + 验证可用」，真正要管理的是「应用级资源」——目前是 Redis 单例。

关键点：`engine.dispose()` 要在关停时调用，把池里所有连接还给 MySQL，避免进程退出
时留下「半开连接」让 MySQL 侧 wait_timeout 才回收。这是「优雅关闭」的一部分。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routers import catalog, chat, conversations, health
from db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭钩子：启动时验证依赖，关闭时释放连接池。"""
    # 启动：这里可以预热 DB 连接、检查迁移状态等。
    # 目前引擎是惰性连接，无需显式预热；保留此钩子给未来的迁移/种子数据。
    yield
    # 关闭：释放连接池，优雅归还所有 MySQL 连接。
    engine.dispose()


app = FastAPI(
    title="Ollama Chatbot API",
    description="把原来的 Streamlit 本地应用后端化：FastAPI + MySQL + Redis + Ollama。",
    version="0.1.0",
    lifespan=lifespan,
)

# 注册路由。每个模块一个 router，职责清晰。
app.include_router(health.router)
app.include_router(catalog.router)
app.include_router(conversations.router)
app.include_router(chat.router)


@app.get("/")
def root():
    """根路径，方便 curl 一眼确认服务活着。"""
    return {"service": "ollama-chatbot-api", "docs": "/docs", "health": "/health"}
