#!/usr/bin/env bash
# 容器冒烟：在一个真实 Docker 环境里把整套 docker-compose 拉起来，逐项验证。
#
# 为什么要有它
# ------------
# 本机没有 Docker 也没有 WSL，所以「容器化部署」一直是这个项目唯一没被真跑过的部分。
# 上一轮补了 19 条部署清单静态校验（读文件比对文本），它们能拦住「配置写错了」，
# 但拦不住「配置对了、跑起来却不对」——比如镜像里到底有没有 .env、
# 容器能不能解析 host.docker.internal、容器里的 MySQL 表到底是不是 utf8mb4。
#
# GitHub 的 runner 自带 Docker，于是把这件事放到 CI 里做：每次 push 都真跑一遍。
# 同一个脚本在云主机上也能跑 —— 那就是**上线验收脚本**：
#   bash deploy.sh && bash .github/scripts/container_smoke.sh
#
# 用法
# ----
#   bash .github/scripts/container_smoke.sh              # 全自动：准备 .env、起假 Ollama、跑完清理
#   KEEP=1 bash .github/scripts/container_smoke.sh       # 失败时保留容器，便于进去排查
#   PYTHON=python3 bash .github/scripts/container_smoke.sh
#
# 收尾的规矩（重要，涉及数据安全）
# -------------------------------
# 只有「服务是本次运行自己拉起来的」才会 `docker compose down -v` 清干净（CI 需要）。
# 如果启动前服务已经在跑——典型场景是刚在云主机上 `deploy.sh` 完，拿本脚本做验收——
# 就只做断言、结束时不动服务，**不会删数据卷**。因为 `down -v` 会删掉 mysql_data。
#
# 它会做什么
# ----------
#   1. 检查 docker / docker compose 可用
#   2. 准备 .env（缺密码时用文档里推荐的那条命令生成）
#   3. docker compose config 校验语法与变量插值 + 记录服务是否已在运行
#   4. 起一个假 Ollama 绑在 0.0.0.0:11434（容器要通过 host.docker.internal 访问它）
#   5. docker compose up -d --build
#   6. 容器内断言：凭据没进镜像 / 非 root / 数据库端口没暴露到宿主机
#   7. 等就绪 + 灌种子数据（必须 exec 进容器，因为 MySQL 刻意没对宿主机开端口）
#   8. 从宿主机打发布端口跑端到端断言（tests/e2e/container_smoke.py）
#   9. 失败时打印容器日志与状态，然后按上面的规矩收尾
#
# 一个反复出现的写法约定（本脚本里刻意保持）：**所有检查都 fail-closed**。
# 不要写 `X="$(cmd | tail -1)"; [[ "$X" != "bad" ]]` 这种形式 ——
# `$()` 的退出码来自管道最后一个命令（tail），cmd 失败时 X 为空，
# 于是「拿不到结果」会被当成「结果正常」，检查静默失效。
# 这里的做法是让被测命令显式输出一个可判定的标记，拿不到标记就报错。

set -euo pipefail

cd "$(dirname "$0")/../.."

PYTHON="${PYTHON:-python3}"
# 容器内一律用 `python`：python:3.11-slim 保证有它，而宿主机的解释器名字
# （python3.12 / 绝对路径等）未必存在于容器里。两者分开，避免一类难懂的报错。
PY_IN="python"
FAKE_PORT=11434
FAKE_LOG="/tmp/fake_ollama.log"
FAKE_PID=""
# 本次运行**之前**这套服务是不是已经在跑了。
# 这个标志是为了防止一个会毁数据的坑：脚本末尾要 `docker compose down -v`
# 收尾（CI 里必须清干净），但 `-v` 会删掉 mysql 数据卷。如果用户在云主机上
# 刚 `deploy.sh` 完再拿本脚本做验收，无条件 down -v 就等于把线上库删了。
# 所以：启动前已在跑 → 不动它，只做断言。
PRE_EXISTING=0

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m[WARN] %s\033[0m\n' "$1"; }
die()  { printf '\n\033[1;31m[FAIL] %s\033[0m\n' "$1" >&2; exit 1; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }

dump_diagnostics() {
  docker compose ps || true
  echo "--- api 日志（末 60 行）---"
  docker compose logs --tail=60 api || true
  echo "--- mysql 日志（末 40 行）---"
  docker compose logs --tail=40 mysql || true
  echo "--- 假 Ollama 日志（末 20 行）---"
  tail -20 "$FAKE_LOG" || true
}

# ── 收尾 ───────────────────────────────────────────────
# 只有「本次运行是我们自己拉起来的」才收：CI 里必须清干净，
# 而线上验收时绝不能碰别人正在跑的服务和数据卷。
cleanup() {
  local rc=$?
  if [[ "${KEEP:-0}" == "1" ]]; then
    warn "KEEP=1：保留容器与假 Ollama 不清理（排查完记得 docker compose down -v）"
    return $rc
  fi
  if [[ "$PRE_EXISTING" == "1" ]]; then
    warn "服务在本次运行前就已经在跑，跳过 down（不会动你的数据卷）"
  else
    docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  [[ -n "$FAKE_PID" ]] && kill "$FAKE_PID" 2>/dev/null || true
  return $rc
}
trap cleanup EXIT

# ── 1. 前置检查 ────────────────────────────────────────
log "1/8 前置检查"
command -v docker >/dev/null 2>&1 || die "没有 docker。安装：curl -fsSL https://get.docker.com | sh"
docker info >/dev/null 2>&1 || die "docker 守护进程不可用（或当前用户不在 docker 组）"
docker compose version >/dev/null 2>&1 || die "缺少 docker compose 插件（要 docker compose，不是 docker-compose）"
command -v "$PYTHON" >/dev/null 2>&1 || die "找不到 $PYTHON"
"$PYTHON" -c "import requests" 2>/dev/null || die "宿主机 $PYTHON 缺 requests：$PYTHON -m pip install requests"
echo "  docker:  $(docker --version)"
echo "  compose: $(docker compose version --short 2>/dev/null || echo '?')"
echo "  python:  $("$PYTHON" --version 2>&1)"

# ── 2. 准备 .env ───────────────────────────────────────
log "2/8 准备 .env"
if [[ ! -f .env ]]; then
  cp .env.prod.example .env
  echo "  已从 .env.prod.example 生成 .env"
fi

# 只有密码为空时才生成。这样在真实服务器上（deploy.sh 已经填过密码）重跑本脚本，
# 不会把线上密码换掉 —— 它得能安全地当验收脚本用。
set +u
PW_NOW="$(grep -E '^MYSQL_ROOT_PASSWORD=' .env | head -1 | cut -d= -f2-)"
set -u
if [[ -z "$PW_NOW" ]]; then
  # 这里**故意**用 base64 而不是文档推荐的 hex。
  # base64 的字符集含 `+` `/` `=`（hex 只有 0-9a-f），是两者里更"危险"的那个：
  # 密码会被拼进 DATABASE_URL，这些字符真出事也是先出在它身上。
  # 所以 CI 用这个更难的情况去撞——如果哪天改动让 base64 密码不再安全，
  # 这里会红，而不是等用户上云那天才发现。
  PW="$(openssl rand -base64 24)"
  # 用 | 当分隔符：base64 的字符集是 A-Za-z0-9+/=，里面没有 |，不会破坏替换。
  sed -i "s|^MYSQL_ROOT_PASSWORD=.*|MYSQL_ROOT_PASSWORD=${PW}|" .env
  echo "  已生成随机密码（长度 ${#PW}，不打印内容）"
else
  echo "  沿用 .env 里已有的密码（长度 ${#PW_NOW}）"
fi
grep -qE '^MYSQL_ROOT_PASSWORD=.+$' .env || die ".env 里的 MYSQL_ROOT_PASSWORD 是空的"
# 含 @ 的密码会让 DATABASE_URL 解析走偏（实测：host 会变成 xxx@mysql，连不上）。
# 这类字符不会在 compose 报错，只会在容器里变成一个难懂的解析错误，所以提前挡住。
if grep -qE '^MYSQL_ROOT_PASSWORD=.*@' .env; then
  die "密码里含 @，会破坏 DATABASE_URL 解析（host 会被解析错）。改用：openssl rand -hex 24"
fi

# ── 3. 校验 compose 配置 ───────────────────────────────
log "3/8 校验 compose 配置（语法 + 变量插值）"
docker compose config >/dev/null || die "docker compose config 失败（YAML 语法或变量插值有问题）"
docker compose config | grep -q 'host-gateway' \
  || die "compose 里没有 extra_hosts: host-gateway，容器将无法解析 host.docker.internal"
ok "compose 配置可解析，变量插值正常，extra_hosts 已声明"

# 记录「本次运行之前服务是否已在跑」。必须在 up 之前判断 —— 这一步决定了
# 最后要不要 down -v（线上验收时不能碰别人的服务，更不能删数据卷）。
if [[ -n "$(docker compose ps -q 2>/dev/null)" ]]; then
  PRE_EXISTING=1
  warn "这套服务当前已经在运行：本次只做断言，结束时不会 down、不会动数据卷"
fi

# ── 4. 起假 Ollama ─────────────────────────────────────
log "4/8 启动假 Ollama（绑 0.0.0.0:${FAKE_PORT}）"
# 必须绑 0.0.0.0：容器里的 host.docker.internal 解析到的是宿主机在 docker 网桥上的
# 地址（如 172.17.0.1），不是回环 127.0.0.1。只绑回环的话宿主机 curl 得通、
# 容器连不上，报错还是 "Connection refused"，很容易误判成服务没起来。
FAKE_OLLAMA_HOST=0.0.0.0 FAKE_OLLAMA_PORT="${FAKE_PORT}" \
  "$PYTHON" tests/e2e/fake_ollama.py > "$FAKE_LOG" 2>&1 &
FAKE_PID=$!

STARTED=0
for _ in $(seq 1 40); do
  if "$PYTHON" -c "
import socket, sys
s = socket.socket(); s.settimeout(0.3)
sys.exit(0 if s.connect_ex(('127.0.0.1', ${FAKE_PORT})) == 0 else 1)
" 2>/dev/null; then STARTED=1; break; fi
  sleep 0.25
done
if [[ "$STARTED" != "1" ]]; then
  cat "$FAKE_LOG" || true
  die "假 Ollama 没起来（端口 ${FAKE_PORT} 被占用？）"
fi
ok "假 Ollama 在 0.0.0.0:${FAKE_PORT} 监听（pid=${FAKE_PID}）"

# ── 5. 构建并启动整套服务 ──────────────────────────────
log "5/8 构建镜像并启动（mysql / redis 健康后 api 才启动）"
docker compose up -d --build
docker compose ps

# ── 6. 容器内断言 ──────────────────────────────────────
# 这些只能用 docker CLI 看，HTTP 层看不见，所以放在编排脚本而不是 python 断言里。
log "6/8 容器内断言"

# ① 凭据没有进镜像：.dockerignore 是否真的生效。
#    注意这条和静态校验的区别：静态校验证明「.dockerignore 里写了 .env」，
#    这里证明「镜像里确实没有 .env」。CRLF 之类的坑会让前者绿、后者红。
#    刻意让容器里的脚本输出一个固定前缀，拿不到就说明 exec 本身失败了 ——
#    否则「exec 失败」会被当成「没有泄漏」而静默通过。
LEAK_LINE="$(docker compose exec -T api "$PY_IN" -c "
import os
paths = ('/app/.env', '/app/.git', '/app/tests', '/app/app.py', '/app/sentiment_analysis')
leaked = [p for p in paths if os.path.exists(p)]
print('LEAKCHECK=' + (','.join(leaked) if leaked else 'CLEAN'))
" 2>/dev/null | tr -d '\r' | grep '^LEAKCHECK=' | tail -1)" || true

[[ -n "$LEAK_LINE" ]] || die "拿不到容器内检查结果（api 容器没起来，或 exec 失败）"
LEAKED="${LEAK_LINE#LEAKCHECK=}"
[[ "$LEAKED" == "CLEAN" ]] \
  || die "以下路径不该出现在镜像里却被拷进去了：$LEAKED（.dockerignore 没生效？）"
ok "镜像里没有 .env / .git / tests / app.py / sentiment_analysis"

# ② 非 root 运行（Dockerfile 里的 USER appuser 是否真的生效）
UID_IN="$(docker compose exec -T api "$PY_IN" -c "print(__import__('os').getuid())" 2>/dev/null | tr -d '\r' | tail -1)" || true
[[ "$UID_IN" =~ ^[0-9]+$ ]] || die "拿不到容器内的 uid（exec 失败？输出：'$UID_IN'）"
[[ "$UID_IN" != "0" ]] || die "容器以 root 运行，Dockerfile 的 USER appuser 没生效"
ok "容器内 uid=${UID_IN}（非 root）"

# ③ 数据库 / 缓存端口没有映射到宿主机，只有 api 暴露
for svc in mysql redis; do
  cid="$(docker compose ps -q "$svc")"
  [[ -n "$cid" ]] || die "拿不到 $svc 的容器 id"
  ports="$(docker inspect --format '{{json .NetworkSettings.Ports}}' "$cid")"
  if echo "$ports" | grep -q 'HostPort'; then
    die "$svc 的端口被映射到了宿主机：$ports（公网上等于对外开了一个数据库端口）"
  fi
  ok "$svc 未向宿主机暴露端口（$ports）"
done
apid="$(docker compose ps -q api)"
[[ -n "$apid" ]] || die "拿不到 api 的容器 id"
api_ports="$(docker inspect --format '{{json .NetworkSettings.Ports}}' "$apid")"
echo "$api_ports" | grep -q 'HostPort' \
  || die "api 没有映射端口，外部访问不到（ports=$api_ports）"
ok "api 已向宿主机暴露端口（$api_ports）"

# ── 7. 等就绪 + 灌种子数据 ─────────────────────────────
log "7/8 等 API 就绪"
API_PORT_EFFECTIVE="$(grep -E '^API_PORT=' .env | head -1 | cut -d= -f2- || true)"
API_PORT_EFFECTIVE="${API_PORT_EFFECTIVE:-8000}"

READY=0
for i in $(seq 1 90); do
  CODE="$("$PYTHON" - "$API_PORT_EFFECTIVE" <<'PY' 2>/dev/null || echo 0
import sys, urllib.request
try:
    print(urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/health/ready", timeout=3).status)
except Exception:
    print(0)
PY
)"
  if [[ "$CODE" == "200" ]]; then READY=1; ok "第 ${i} 次探测：已就绪"; break; fi
  sleep 2
done

if [[ "$READY" != "1" ]]; then
  log "没等到就绪，打印诊断信息"
  dump_diagnostics
  die "服务未就绪（常见原因见 docs/DEPLOY.md 的排查一节）"
fi

# 种子数据只能从容器内部灌：MySQL 刻意没有映射端口到宿主机，宿主机连不上它。
# 这本身也顺带验证了「容器之间能互通、api 容器拿得到 DATABASE_URL」。
log "灌种子数据（exec 进 api 容器执行）"
docker compose exec -T api "$PY_IN" - <<'PY'
from db.models import Model, User
from db.session import SessionLocal

db = SessionLocal()
try:
    if not db.query(User).count():
        db.add(User(username="smoke", email="smoke@example.com", plan="free"))
    if not db.query(Model).count():
        db.add(Model(name="llama3.2", provider="ollama", param_size="3B", context_window=8192))
    db.commit()
    print(f"已灌种子：users={db.query(User).count()} models={db.query(Model).count()}")
finally:
    db.close()
PY

# ── 8. 端到端断言 ──────────────────────────────────────
log "8/8 从宿主机打发布端口，跑端到端断言"
set +e
API_BASE="http://127.0.0.1:${API_PORT_EFFECTIVE}" "$PYTHON" tests/e2e/container_smoke.py
RC=$?
set -e

if [[ "$RC" -ne 0 ]]; then
  log "断言失败，打印诊断信息"
  dump_diagnostics
  exit "$RC"
fi

log "容器冒烟全部通过"
echo "  这套服务是真的在 Docker 里跑起来过的：镜像能构建、容器之间能互通、"
echo "  容器内的 MySQL 表是 utf8mb4、数据库端口没有暴露到宿主机。"
