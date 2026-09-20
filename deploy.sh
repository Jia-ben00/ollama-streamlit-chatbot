#!/usr/bin/env bash
# 云主机上一键部署脚本（方案阶段 C：把后端推到公网）。
#
# 用法（在项目根目录）：
#   cp .env.prod.example .env && vi .env      # 填 MYSQL_ROOT_PASSWORD 等
#   bash deploy.sh                            # 拉代码 + 构建 + 起服 + 轮询健康检查
#   bash deploy.sh --no-pull                  # 不拉代码，只重建（CI / 重复部署用）
#   bash deploy.sh --proxy                    # 连 nginx 反代一起起（上线推荐，见 docs/DEPLOY.md §6）
#
# 环境变量（都有默认值，一般不用动）：
#   READY_TIMEOUT=180    等 API 就绪的总秒数
#   READY_INTERVAL=2     两次探测之间的间隔秒数
#
# 设计原则：每一步都可重复执行（幂等）；失败时打印能直接照抄的排查命令，
# 而不是丢一句 "deploy failed"。
#
# ⚠️ 这个脚本长期是「从没被执行过」的状态 —— CI 直接跑 container_smoke.sh，
#    静态校验只把它当文本读（查行尾、查字符串）。本仓库已经栽过两次同类洞
#    （nginx_check.py、容器冒烟）：「读起来没问题」和「跑起来没问题」是两件事。
#    现在它被两处钉住：
#      1. tests/test_deploy_script.py —— 替身 docker，本机与 CI 都能秒级跑完每条分支；
#      2. container_smoke.sh 第 5 步 —— 在真 Docker 上真跑一遍（和服务器上同一条命令）。

set -euo pipefail

cd "$(dirname "$0")"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }
die() { printf '\n\033[1;31m[FAIL] %s\033[0m\n' "$1" >&2; exit 1; }

PULL=1
WITH_PROXY=0
for arg in "$@"; do
  case "$arg" in
    --no-pull) PULL=0 ;;
    --proxy)   WITH_PROXY=1 ;;
    # 不认识的参数要报错，不能当没看见：`--prox` 打错一个字母，
    # 静默按「不带反代」部署下去，结果就是入口没起来而脚本说成功了。
    *) die "不认识的参数：$arg（只支持 --no-pull / --proxy）" ;;
  esac
done

# ── 0. 读 .env 的小工具 ────────────────────────────────
#
# 必须 fail-closed。以前每处都写成
#     PW="$(grep -E '^MYSQL_ROOT_PASSWORD=' .env | head -1 | cut -d= -f2-)"
# 而本脚本开头是 set -euo pipefail：`.env` 里没有这一行时 grep 返回 1，
# pipefail 让整条管道失败，**赋值语句直接触发 set -e 退出**。
# 现象是：连"==> 检查前置条件"之后一行输出都没有、退出码 1 ——
# 比它自己想避免的"丢一句 deploy failed"还难查（实测确认过）。
# （同一段里 API_PORT 那处写了 `|| true`，所以只有密码这处会静默退出。）
#
# 现在统一走这里：管道用 `|| true` 兜住，拿不到就是空串，由调用方显式判空。
read_env() {
  local key="$1" line
  line="$(grep -E "^${key}=" .env 2>/dev/null | head -1 || true)"
  printf '%s' "${line#*=}"
}

# ── 1. 前置检查 ────────────────────────────────────────
log "检查前置条件"

command -v docker >/dev/null 2>&1 || die "没装 Docker。安装：curl -fsSL https://get.docker.com | sh"
docker compose version >/dev/null 2>&1 || die "docker compose 插件不可用（需要 docker compose，不是 docker-compose）"
docker info >/dev/null 2>&1 || die "Docker 守护进程没跑，或者当前用户不在 docker 组（试 sudo -i，或 sudo usermod -aG docker \$USER 后重新登录）"

[[ -f .env ]] || die "缺少 .env。先执行：cp .env.prod.example .env 然后填好 MYSQL_ROOT_PASSWORD"

# 密码不能是空、也不能还是默认占位值 —— 公网部署里这是最容易被扫到的洞。
# 第一句同时兜住「整行不存在」（以前是静默退出）。
PW="$(read_env MYSQL_ROOT_PASSWORD)"
[[ -n "$PW" ]] || die ".env 里没有 MYSQL_ROOT_PASSWORD（整行缺失或值为空），先填一个强密码：openssl rand -hex 24"
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

PORT="$(read_env API_PORT)"
PORT="${PORT:-8000}"
PROXY_PORT="$(read_env PROXY_HTTP_PORT)"
PROXY_PORT="${PROXY_PORT:-80}"

# ── 1b. 启用反代时，API 必须只绑回环 ───────────────────
#
# compose 把 api 的发布端口写成 ${API_BIND:-0.0.0.0}:${API_PORT:-8000}:8000，
# 默认 0.0.0.0 就是「对全世界开放」。这么一来即使起了反代，外面照样能直连 8000：
# 反代那一层（HTTPS、限流、真实 IP、SSE 防缓冲）全被绕过，
# 「只放行 80、撤掉 8000」也就只剩安全组那一道，撤错一次就静默暴露。
#
# 与其在文档里写「记得改 API_BIND」，不如在这里卡住 —— 而且是在 up 之前卡住，
# 不留下一个"半配置"的、看起来像成功了的部署。
if [[ "$WITH_PROXY" == "1" ]]; then
  BIND="$(read_env API_BIND)"
  BIND="${BIND:-0.0.0.0}"
  if [[ "$BIND" != "127.0.0.1" ]]; then
    die "启用了 --proxy，但 .env 里 API_BIND=${BIND}（不是 127.0.0.1）。
       这样 ${PORT} 端口仍然对整个公网开放，反代（HTTPS / 限流 / SSE 防缓冲）等于白配。
       改法：把 .env 里的 API_BIND 改成 127.0.0.1 再重跑。
       （想继续直连 ${PORT} 调试就先不要加 --proxy。）"
  fi
fi

# ── 1b2. 反代这一层是明文还是 HTTPS：**看配置，不看命名** ──────────
#
# 判断依据是「待渲染的那份模板里有没有 ssl_certificate」，而不是「目录名里有没有 tls」：
# 目录名随便起，而「我到底在对外提供明文还是 TLS」只有配置能回答。
# compose 侧的开关是 NGINX_TEMPLATES_DIR（见 docker-compose.yml 的 proxy 服务）。
TEMPLATES_DIR="${NGINX_TEMPLATES_DIR:-$(read_env NGINX_TEMPLATES_DIR)}"
TEMPLATES_DIR="${TEMPLATES_DIR:-./deploy/nginx/templates}"
TLS_MODE=0
if [[ "$WITH_PROXY" == "1" ]]; then
  [[ -d "$TEMPLATES_DIR" ]] || die "NGINX_TEMPLATES_DIR=${TEMPLATES_DIR} 不存在。
       反代容器会挂到一个空目录上，然后服务镜像自带的默认站点（也是 200，很难看出不对）。
       仓库里有两份：./deploy/nginx/templates（明文）、./deploy/nginx/tls（HTTPS）。"
  # 目录里没有 *.template 时 glob 不展开、grep 静默失败 —— 这没问题（就当不是 TLS），
  # 上面那条目录检查已经兜住了「挂错地方」这一类。
  if grep -qs 'ssl_certificate ' "$TEMPLATES_DIR"/*.template 2>/dev/null; then
    TLS_MODE=1
  fi
fi
HTTPS_PORT="$(read_env PROXY_HTTPS_PORT)"
HTTPS_PORT="${HTTPS_PORT:-443}"

# 证书必须在 up **之前**检查：少了证书文件，nginx 会在容器里当场退出，
# 用户看到的却是「反代起不来 + 一串 nginx 日志」，而且要先等构建跑完才能看到。
# 这两个名字写死在模板里（deploy/nginx/tls/default.conf.template 那两行），
# 所以这里查的正是它接下来会去读的那两个。
if [[ "$TLS_MODE" == "1" ]]; then
  CERT_DIR="${TLS_CERT_DIR:-$(read_env TLS_CERT_DIR)}"
  CERT_DIR="${CERT_DIR:-./deploy/nginx/certs}"
  for f in fullchain.pem privkey.pem; do
    [[ -f "${CERT_DIR}/${f}" ]] || die "配置源是 HTTPS（${TEMPLATES_DIR}），但 ${CERT_DIR} 里没有 ${f}。
       nginx 会因此在启动时直接退出（日志里是 cannot load certificate ...）。
       两种解法：
         · 已有证书：让它按这两个固定名字就位（certbot 的 live/<域名>/ 正好是这两个名字），
           再把 .env 里的 TLS_CERT_DIR 指过去；
         · 只想先在本机把这一层验掉：python tests/e2e/nginx_check.py（自签证书，不需要域名）。"
  done
fi

echo "  docker:   $(docker --version)"
echo "  compose:  $(docker compose version --short 2>/dev/null || echo '?')"
echo "  密码长度: ${#PW} 字符（不打印内容）"
if [[ "$WITH_PROXY" == "1" && "$TLS_MODE" == "1" ]]; then
  echo "  入口:     HTTPS 反代 :${HTTPS_PORT} → api:8000（明文 :${PROXY_PORT} 只做 301 跳转）"
elif [[ "$WITH_PROXY" == "1" ]]; then
  echo "  入口:     反代 :${PROXY_PORT} → api:8000（api 只在宿主机回环上发布）"
else
  echo "  入口:     直连 api :${PORT}（没有反代）"
fi

# ── 1c. 提醒构建会走哪个 pip 源（「数小时」和「两分钟」的分界）──
#
# 为什么不写进文档就算了：装依赖那一层是构建里最慢的一层，而这个脚本**马上就要**
# 进那一层。本机实测（2026-09-19，国内网络）：官方 PyPI 只有 ~45 KB/s，
# 那一层跑了 3.7 小时还没完（pyarrow 一个包 2.6 小时）；换国内镜像后同一层约 2 分钟。
# 提醒放在这里才是它唯一有用的位置 —— 现在还能 Ctrl-C 去改 .env，
# 等构建跑起来之后再看到，就只剩等着了（而且「卡住了」和「就是这么慢」在屏幕上
# 长得一模一样，只能靠等几小时来区分）。
#
# 只提醒、不拦截：CI 里的 .env 是从 .env.prod.example 复制的（PIP_INDEX_URL 为空），
# 拦下来会让每次 push 都红 —— 而 CI 的 runner 访问官方 PyPI 是快的，它不该被拦。
#
# 取值顺序必须和 compose 一致：**shell 环境优先于 .env**。
# （实测踩过：`export PIP_INDEX_URL=<镜像>` 时 compose 走了镜像，而这里只读 .env，
#   于是脚本一边说「将走官方源、可能要几小时」，一边构建明明用的是镜像 ——
#   一句会骗人的提醒比没有提醒更糟。）
PIP_SRC="${PIP_INDEX_URL:-$(read_env PIP_INDEX_URL)}"
case "$PIP_SRC" in
  ""|*pypi.org*)
    printf '\n\033[1;33m[提示] 构建将走官方 PyPI（%s）—— 国内网络下这一层可能要几个小时。\n' \
      "${PIP_SRC:-模板里留空 = 官方源}" >&2
    printf '       实测：官方源 ~45 KB/s（3.7 小时没跑完），国内镜像同一层约 2 分钟。\n' >&2
    printf '       想加速：往 .env 里加一行再重跑本脚本（已完成的层会被 Docker 复用）——\n' >&2
    printf '         PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple\n\033[0m' >&2 ;;
  *)
    echo "  pip 源:   ${PIP_SRC}" ;;
esac

# ── 2. 拉代码（可选）───────────────────────────────────
if [[ "$PULL" == "1" && -d .git ]]; then
  log "拉取最新代码"
  git pull --ff-only || die "git pull 失败：本地有未提交改动或分支分叉，先手动处理"
fi

# ── 3. 构建并启动 ──────────────────────────────────────
# 反代在 compose 里是 `profiles: ["proxy"]` 服务：默认不启动，
# 要多加 --profile proxy 才参与。所以这个分支不能省 ——
# 少了它，脚本会"成功"但入口是 8000 而不是反代。
if [[ "$WITH_PROXY" == "1" ]]; then
  log "构建镜像并启动（含 nginx 反代；mysql 健康后 api 才启动）"
  docker compose --profile proxy up -d --build
else
  log "构建镜像并启动（mysql 会等健康检查通过后 api 才启动）"
  docker compose up -d --build
fi

# ── 4. 轮询健康检查 ────────────────────────────────────
READY_TIMEOUT="${READY_TIMEOUT:-180}"
READY_INTERVAL="${READY_INTERVAL:-2}"
# 间隔至少 1 秒：0 会让"轮询"变成刷屏，日志反而没法看。
[[ "$READY_INTERVAL" =~ ^[0-9]+$ ]] || READY_INTERVAL=2
(( READY_INTERVAL >= 1 )) || READY_INTERVAL=1
[[ "$READY_TIMEOUT" =~ ^[0-9]+$ ]] || READY_TIMEOUT=180
# 次数由超时算出来，这样「打印的上限」和「实际会等多久」是同一个数。
# 以前这里写死 90 次 × sleep 2s，却对外宣称"最多 180 秒" ——
# 每次探测本身还要花时间（exec 起个 python 进程 + 3 秒超时），
# 真实上界能到分钟级，和打印出来的承诺不是一回事。
ATTEMPTS=$(( READY_TIMEOUT / READY_INTERVAL ))
(( ATTEMPTS >= 1 )) || ATTEMPTS=1

log "等待 API 就绪（轮询 http://127.0.0.1:8000/health/ready，最多 ${READY_TIMEOUT} 秒）"
READY=0
for i in $(seq 1 "$ATTEMPTS"); do
  CODE="$(docker compose exec -T api python -c "
import sys, urllib.request
try:
    print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3).status)
except Exception:
    print(0)
" 2>/dev/null | tr -d '\r' | tail -1 || echo 0)"
  if [[ "$CODE" == "200" ]]; then READY=1; echo "  第 ${i} 次探测：就绪 ✅"; break; fi
  sleep "$READY_INTERVAL"
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

SCHEME="http"
if [[ "$WITH_PROXY" == "1" && "$TLS_MODE" == "1" ]]; then
  SCHEME="https"
  HOST_PORT="$HTTPS_PORT"
  ENTRY_NOTE="  反代已启用（HTTPS）：证书来自 ${CERT_DIR}，外界只能经 :${HTTPS_PORT} 进来；
  明文 :${PROXY_PORT} 只回 301 跳转（以及 ACME 挑战，证书续期靠它）。"
  SG_NOTE="  安全组放行 ${HTTPS_PORT} 与 ${PROXY_PORT}（80 要留给跳转和证书续期），
  「不要」放行 ${PORT}。"
elif [[ "$WITH_PROXY" == "1" ]]; then
  HOST_PORT="$PROXY_PORT"
  ENTRY_NOTE="  反代已启用：api 只在容器内网与宿主机回环上可达，外界只能经 :${PROXY_PORT} 进来。"
  SG_NOTE="  安全组只放行 ${PROXY_PORT}，「不要」放行 ${PORT}。
  要加 HTTPS：.env 里 NGINX_TEMPLATES_DIR=./deploy/nginx/tls、证书按 fullchain.pem +
  privkey.pem 就位、TLS_CERT_DIR 指过去，再重跑本脚本（见 docs/DEPLOY.md §6.2）。"
else
  HOST_PORT="$PORT"
  ENTRY_NOTE="  未启用反代：外界直连 api :${PORT}（HTTPS / 限流 / 真实 IP 要自己另配）。"
  SG_NOTE="  如果公网 curl 不通但本机通 —— 那是云主机安全组没放行 ${PORT} 端口，
  不是代码问题。去控制台的安全组/防火墙里加一条入站规则。"
fi

# 验收命令按部署形态拼：HTTPS 下自签证书要 --ca（脚本不吃 curl 的 -k），
# 而且证书一般是签给域名的 —— 用 IP 跑会验签失败，所以有域名就用域名。
CURL_EXTRA=""
PCHECK_URL="${SCHEME}://${IP}:${HOST_PORT}"
PCHECK_HINT=""
if [[ "$TLS_MODE" == "1" ]]; then
  CURL_EXTRA=" -k"
  SN="$(read_env SERVER_NAME)"
  if [[ -n "$SN" && "$SN" != "_" ]]; then PCHECK_URL="https://${SN}"; fi
  PCHECK_HINT="
       （自签证书要加 --ca <证书路径>；上面的 -k 是 curl 的写法，脚本不吃。
         证书是签给域名的，所以这里默认用域名跑，别用 IP。）"
fi

# 打印的「停止服务」必须与部署形态一致。
# `docker compose down` **不会**停掉属于非激活 profile 的服务：用 --proxy 部署时，
# 反代容器会原地留着、继续占着 80/443，连网络都删不掉（实测报
# `Resource is still in use`），下一次 up 就会端口冲突。所以这里按形态给对的那条命令。
COMPOSE_SVC="docker compose"
if [[ "$WITH_PROXY" == "1" ]]; then
  COMPOSE_SVC="docker compose --profile proxy"
fi

cat <<EOF

  本机验证： curl -s${CURL_EXTRA} ${SCHEME}://127.0.0.1:${HOST_PORT}/health | python -m json.tool
  公网验证： curl -s${CURL_EXTRA} ${SCHEME}://${IP}:${HOST_PORT}/health | python -m json.tool
  API 文档： ${SCHEME}://${IP}:${HOST_PORT}/docs
  实时日志： docker compose logs -f api
  停止服务： ${COMPOSE_SVC} down          （保留数据）
  清库重来： ${COMPOSE_SVC} down -v       （会删 mysql 数据卷）

${ENTRY_NOTE}
${SG_NOTE}

  ⚠️ 上面两条 curl 都只是「从这台机器出发」，只能证明服务起来了，
     证明不了公网可访问 —— 而且 SSE 有没有被中间那层攒批，只有从**外面**看才知道。
     在你自己电脑上跑一次（要 requests）：
       python tests/e2e/public_check.py --url ${PCHECK_URL}${PCHECK_HINT}
     本机通、外面也通、逐块到达也是分开的，才叫公网可访问（见 docs/DEPLOY.md §7.5）。

EOF
