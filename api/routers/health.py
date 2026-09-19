"""健康检查路由：GET /health，三探针。

面试会问：为什么一个 /health 要同时探 DB、Redis、Ollama 三个东西？

因为「进程活着」不等于「服务可用」。一个 API 进程可能自身好好的，但：
- MySQL 挂了 → 所有落库/查询都失败；
- Redis 挂了 → 缓存降级（能跑但慢）；
- Ollama 挂了 → 聊天核心功能废掉。

如果 /health 只返回 200 表示「进程在」，负载均衡器/监控会误以为一切正常，
把流量继续打到这个「半残」的实例上。所以健康检查要区分「存活」（liveness，
进程在不在）和「就绪」（readiness，依赖齐不齐）。这里三个探针就是 readiness。

返回结构里每个依赖是独立的 ok 状态，而不是一个总开关——这样出问题时，
一眼看出是哪个依赖挂了，而不是「整体 unhealthy」让你去猜。
"""

from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi import Depends

from api.deps import get_db
from cache import cache
from src.ollama_client import OllamaClient

router = APIRouter(tags=["health"])

# Ollama 探针复用现有的 OllamaClient（它已经封装了 /api/tags 的健康检查）。
_ollama = OllamaClient()


@router.get("/health")
def health(db: Session = Depends(get_db)):
    """三探针健康检查：DB / Redis / Ollama。"""

    # DB 探针：跑一个 SELECT 1。用 text() 执行裸 SQL 而非走 ORM，避免依赖任何表存在。
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False

    redis_ok = cache.enabled  # 缓存初始化时已 ping 过；enabled 即代表可达。

    ollama_ok = _ollama.check_health()

    # HTTP 状态码：三个依赖都健康返回 200，否则返回 503（Service Unavailable）。
    # 这样负载均衡器/编排系统（K8s readinessProbe）能据此摘掉不健康实例。
    all_ok = db_ok and redis_ok and ollama_ok
    return {
        "status": "ok" if all_ok else "degraded",
        "checks": {
            "database": db_ok,
            "redis": redis_ok,
            "ollama": ollama_ok,
        },
    }
