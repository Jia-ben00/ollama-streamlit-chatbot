# 后端 API 的容器镜像。
#
# 面试会问「为什么分多个阶段（multi-stage）？」：
# 构建阶段用完整 Python 镜像装依赖、编译，运行阶段用精简镜像只放运行产物，
# 最终镜像不包含编译器、pip 缓存、源码里的构建中间文件，体积从 GB 级降到几百 MB。
# 体积小 → 拉取快、攻击面小、部署快。

# ── 构建阶段 ──────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# 先复制依赖清单再装，是为了利用 Docker 的层缓存：
# 只要 requirements 不变，这层就能复用，不用每次改代码都重装一遍依赖。
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── 运行阶段 ──────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# 把构建阶段装好的依赖从 /install 拷过来（--prefix 装到独立目录，拷贝干净）。
COPY --from=builder /install /usr/local

# 复制应用代码。
COPY . .

# 用非 root 用户跑，减少容器被攻破后的权限危害（最小权限原则）。
RUN useradd -m appuser
USER appuser

# 暴露 FastAPI 默认端口。
EXPOSE 8000

# 启动命令：uvicorn 起 ASGI 服务。
# --host 0.0.0.0 让容器外部能访问（容器内监听 localhost 会外部不可达）。
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
