#!/usr/bin/env bash
# 云主机上一键部署脚本（方案阶段 C：把后端推到公网）。
#
# 用法（在项目根目录）：
#   cp .env.prod.example .env && vi .env      # 填 MYSQL_ROOT_PASSWORD 等
#   bash deploy.sh                            # 拉代码 + 构建 + 起服 + 轮询健康检查
#   bash deploy.sh --no-pull                  # 不拉代码，只重建
#
# 设计原则：每一步都可重复执行（幂等）；失败时打印能直接照抄的排查命令，
# 而不是丢一句 "deploy failed"。

set -euo pipefail

cd "$(dirname "$0")"

PULL=1
[[ "${1:-}" == "--no-pull" ]] && PULL=0

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }
die() { printf '\n\033[1;31m[FAIL] %s\033[0m\n' "$1" >&2; exit 1; }

# ── 1. 前置检查 ────────────────────────────────────────
log "检查前置条件"

command -v docker >/dev/null 2>&1 || die "没装 Docker。安装：curl -fsSL https://get.docker.com | sh"
docker compose version >/dev/null 2>&1 || die "docker compose 插件不可用（需要 docker compose，不是 docker-compose）"
docker info >/dev/null 2>&1 || die "Docker 守护进程没跑，或者当前用户不在 docker 组（试 sudo -i，或 sudo usermod -aG docker \$USER 后重新登录）"

[[ -f .env ]] || die "缺少 .env。先执行：cp .env.prod.example .env 然后填好 MYSQL_ROOT_PASSWORD"

# 密码不能是空、也不能还是默认占位值 —— 公网部署里这是最容易被扫到的洞。
set +u
PW="$(grep -E '^MYSQL_ROOT_PASSWORD=' .env | head -1 | cut -d= -f2-)"
set -u
[[ -n "$PW" ]] || die ".env 里的 MYSQL_ROOT_PASSWORD 是空的，先填一个强密码：openssl rand -hex 24"
[[ "$PW" != "change-me" && "$PW" != "123456" ]] || die "MYSQL_ROOT_PASSWORD 还是默认值，公网部署必须换掉"

# 密码里不能有 `@`。
#
# 原因：compose 把密码直接拼进 api 的 DATABASE_URL：
#   mysql+pymysql://root:<密码>@mysql:3306/chatbot
# 而 URL 解析器在**第一个** @ 处切开 userinfo。实测密码 "ab@cd" 会被解析成
# 密码 "ab" + 主机名 "cd@mysql" —— 容器报的是「找不到主机 cd@mysql」，
# 几乎不可能一眼看出是密码字符的问题。
#
# 推荐 openssl rand -hex 24（字符集 0-9a-f，安全）。
# `openssl rand -base64 24` 实测也能用（/ + = 都能正确解析），只是 hex 更省心；
# 若确实要用含 @ 的密码，得写成 %40（URL 百分号编码）。
if [[ "$PW" == *"@"* ]]; then
  die "MYSQL_ROOT_PASSWORD 里含 @，会让 DATABASE_URL 解析错主机名（实测：host 会变成 xxx@mysql）。
       生成一个不含 @ 的密码：openssl rand -hex 24"
fi
echo "  docker:   $(docker --version)"
echo "  compose:  $(docker compose version --short 2>/dev/null || echo '?')"
echo "  密码长度: ${#PW} 字符（不打印内容）"

# ── 2. 拉代码（可选）───────────────────────────────────
if [[ "$PULL" == "1" && -d .git ]]; then
  log "拉取最新代码"
  git pull --ff-only || die "git pull 失败：本地有未提交改动或分支分叉，先手动处理"
fi

# ── 3. 构建并启动 ──────────────────────────────────────
log "构建镜像并启动（mysql 会等健康检查通过后 api 才启动）"
docker compose up -d --build

# ── 4. 轮询健康检查 ────────────────────────────────────
PORT="$(grep -E '^API_PORT=' .env | head -1 | cut -d= -f2- || true)"
PORT="${PORT:-8000}"

log "等待 API 就绪（轮询 http://127.0.0.1:${PORT}/health/ready，最多 180 秒）"
READY=0
for i in $(seq 1 90); do
  CODE="$(docker compose exec -T api python -c "
import sys, urllib.request
try:
    print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3).status)
except Exception:
    print(0)
" 2>/dev/null | tr -d '\r' | tail -1 || echo 0)"
  if [[ "$CODE" == "200" ]]; then READY=1; echo "  第 ${i} 次探测：就绪 ✅"; break; fi
  sleep 2
done

log "依赖项状态"
docker compose exec -T api python -c "
import json, urllib.request
print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8000/health')), ensure_ascii=False, indent=2))
" 2>/dev/null || echo "  （取不到 /health，见下面的日志）"

if [[ "$READY" != "1" ]]; then
  log "没等到就绪，打印最后 50 行 api 日志"
  docker compose logs --tail=50 api
  docker compose logs --tail=30 mysql
  die "部署未就绪。常见原因见 docs/DEPLOY.md 的「排查」一节"
fi

# ── 5. 收尾提示 ────────────────────────────────────────
log "部署完成"
IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo '<服务器公网IP>')"
cat <<EOF

  本机验证： curl -s http://127.0.0.1:${PORT}/health | python -m json.tool
  公网验证： curl -s http://${IP}:${PORT}/health | python -m json.tool
  API 文档： http://${IP}:${PORT}/docs
  实时日志： docker compose logs -f api
  停止服务： docker compose down          （保留数据）
  清库重来： docker compose down -v       （会删 mysql 数据卷）

  如果公网 curl 不通但本机通 —— 那是云主机安全组没放行 ${PORT} 端口，
  不是代码问题。去控制台的安全组/防火墙里加一条入站规则。

EOF
