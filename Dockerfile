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

# pip 源可覆盖（默认走官方 PyPI）。
#
# 为什么留这个开关：这一层是构建里**最慢**的一层，而快慢完全取决于 PyPI 通不通。
# 本机实测（2026-09-19，国内网络）：官方 PyPI 只有 ~45 KB/s，这一层跑了
# **3.7 小时**还没完（pyarrow 一个包 2.6 小时）；换成国内镜像后同一层约 2 分钟。
# 差值 20 倍以上，且与本项目代码无关 —— 纯粹是通道问题。
#
# 覆盖方式二选一：
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#   或在 .env 里写 PIP_INDEX_URL=...（compose 会把同名变量透传成 build arg，见 docker-compose.yml）
#
# 默认值刻意保持官方源：这是公开仓库，把某个国内镜像写死成默认，
# 等于让海外和 CI 的构建多绕一圈；而覆盖的成本就是上面那一行。
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --prefix=/install \
        --index-url "${PIP_INDEX_URL}" \
        -r requirements.txt

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
