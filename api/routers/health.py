"""健康检查路由：/health（存活）与 /health/ready（就绪）。

面试会问：为什么一个 /health 要同时探 DB、Redis、Ollama 三个东西？

因为「进程活着」不等于「服务可用」。一个 API 进程可能自身好好的，但：
- MySQL 挂了 → 所有落库/查询都失败；
- Redis 挂了 → 缓存降级（能跑但慢）；
- Ollama 挂了 → 聊天核心功能废掉。

所以健康检查要区分「存活」（liveness，进程在不在）和「就绪」（readiness，依赖齐不齐）：

- `GET /health`：**存活 + 诊断**。只要进程能响应就返回 200，body 里逐项报告依赖状态。
  为什么这里不返回 503：liveness 探针的语义是「要不要重启这个进程」。依赖挂了重启
  进程是没用的（重启一百次 MySQL 也不会回来），反而会造成重启风暴。所以它只回答
  「我还活着吗」，把「依赖好不好」放在 body 里供人排查。
- `GET /health/ready`：**就绪**。三个依赖全通才返回 200，否则 503。
  给「要不要把流量摘掉」这个决策用（K8s readinessProbe / 负载均衡后端探测 /
  部署脚本轮询）。依赖没齐时返回 503，编排系统就不会往这个实例打流量。

返回结构里每个依赖是独立的 ok 状态，而不是一个总开关——这样出问题时，
一眼看出是哪个依赖挂了，而不是「整体 unhealthy」让你去猜。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.deps import get_db
from cache import cache
from src.ollama_client import OllamaClient

router = APIRouter(tags=["health"])

# Ollama 探针复用现有的 OllamaClient（它已经封装了 /api/tags 的健康检查）。
_ollama = OllamaClient()


def _probe(db: Session) -> dict:
    """跑三个探针，返回逐项结果。两个端点共用，避免逻辑写两遍。"""
    # DB 探针：跑一个 SELECT 1。用 text() 执行裸 SQL 而非走 ORM，避免依赖任何表存在。
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False

    return {
        "database": db_ok,
        # 缓存初始化时已 ping 过；enabled 即代表可达。
        "redis": cache.enabled,
        "ollama": _ollama.check_health(),
    }


@router.get("/health")
def health(db: Session = Depends(get_db)):
    """存活 + 诊断：进程能响应就 200，依赖状态放在 body 里逐项报告。"""
    checks = _probe(db)
    return {
        "status": "ok" if all(checks.values()) else "degraded",
        "checks": checks,
    }


@router.get("/health/ready")
def readiness(db: Session = Depends(get_db)):
    """就绪探针：依赖全通才 200，否则 503 —— 供编排系统决定要不要摘流量。"""
    checks = _probe(db)
    all_ok = all(checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ready" if all_ok else "not_ready", "checks": checks},
    )
