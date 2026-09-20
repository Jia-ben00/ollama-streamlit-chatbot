# 部署手册（阶段 C：把后端推到公网）

目标：在一台云主机上把 `api + mysql + redis` 三个容器拉起来，让
`http://<公网IP>:8000/health` 能被外面访问到。
（可选但建议再加一层 nginx 反代，见 §6；启用后对外只开 80/443，8000 从安全组撤掉。）

## 0. 先说清楚现状（诚实标注）

| 做到哪一步了 | 状态 |
|---|---|
| 镜像与编排文件（`Dockerfile` / `docker-compose.yml`） | ✅ 已写好 |
| 部署脚本（`deploy.sh`） | ✅ 已写好，而且**被真跑过**：20 条替身 `docker` 用例走完它的每条分支（`tests/test_deploy_script.py`，进 CI），CI 的第 5 步还会用真 Docker 跑**同一条命令** |
| 编排文件之间的自洽性（compose / Dockerfile / `.dockerignore` / `.env` 模板 / nginx 模板） | ✅ 静态校验守着：`tests/test_deploy_manifest.py`（20 用例）+ `tests/test_nginx_config.py`（14 用例）+ `tests/test_nginx_tls.py`（24 用例，含**两份模板逐字节不漂移**、私钥不进仓库）+ 证明这些守卫真会失败（8 用例），都在 CI 里 |
| 本机跑通整个后端（真 MySQL + 假 Ollama + 真 uvicorn，27 项断言） | ✅ 已验证 |
| **在真实 Docker 里把整套 compose 跑起来** | ✅ **CI 每次 push 真跑**（`.github/workflows/container-smoke.yml`，run `35491236413`：**47 PASS / 0 FAIL**，约 2 分 53 秒）；**2026-09-20 起本机也能真跑**（装上了 Docker Desktop）。镜像能构建、容器之间能互通、容器内 MySQL 的表真是 utf8mb4、密码没被拷进镜像、3306/6379 没暴露 |
| **反代这一层（Nginx + 流式不被攒批）** | ✅ **两份配置都在仓库里，并且都用真 nginx 跑过**：`deploy/nginx/templates/`（明文）与 `deploy/nginx/tls/`（HTTPS）。验收是 `.github/scripts/container_smoke.sh` 第 9 步（`tests/e2e/nginx_check.py` 的 A/B/C/C′/D 五段），**每条通道都带一个必须被判成攒批的反例**。TLS 那半在本机用自签证书真起过；仍然没验的只剩「真域名 + ACME 签发续期」，见 §6.2 |
| **反代路径的「第一条命令」（`deploy.sh --proxy`）** | ✅ **本机真跑过**（2026-09-20）：`.github/scripts/container_smoke.sh` 第 10 步（`tests/e2e/proxy_deploy_check.py`）。它验的正是上面那格**没覆盖**的一段 —— compose 里 `profiles: ["proxy"]` 真的生效、那颗「先探明文、失败再探 HTTPS」的双模式 healthcheck 在 TLS 下真的通过、明文口的 301 跟着跳真的到得了 HTTPS，以及 `public_check.py --ca` 这条分支第一次被执行 |
| **在云主机上对公网提供服务** | ❌ **还没做过** —— 缺一台能跑 Docker Compose 的 Linux 主机 |

最下面那行仍然是唯一没做到的：**还没有一台云主机**。上面的每一行都是这几轮补的，
补的都是同一个东西：**「文件写好了」和「真跑起来是对的」是两件事**。
在此之前「容器」和「反代」这两层的真实验证一直缺位，是拿 CI 的 runner 兜的
（开发机没有 WSL、内存 1G）；2026-09-19 装上 Docker Desktop 之后，这两层在本机也能验了。
但重点不是「本机能验」—— 是**先把「这个脚本从没被执行过」当成缺陷处理掉**，
本机 Docker 只是让这件事更容易。补法分三层，前两层管容器，第三层管反代：

1. **静态校验**（`tests/test_deploy_manifest.py` + `tests/test_nginx_config.py`，
   不需要 Docker，秒级）：凡是「只会在上云第一小时暴露」的配置问题，尽量挪到提交前。
2. **真跑一遍容器**（`.github/scripts/container_smoke.sh`，在 CI 的 Docker 上跑整套
   compose）：静态校验只能证明「文件里写了正确的规则」，证明不了「规则真的生效」。
   比如 `.dockerignore` 里写了 `.env`，但要是文件被检出成 CRLF，规则会静默失效——
   静态校验照样绿，只有真去镜像里 `ls` 才发现密码躺在那里。
3. **真跑一遍反代**（同一个脚本的第 9 步，用真 nginx 镜像 + 仓库那份模板）：
   量 SSE 的真实到达时刻，并且**要求一个反例必须被判成攒批**——否则「正例通过」
   说明不了任何事（一把恒真的尺子也长这样）。

所以这份手册的步骤是「照着做就能上线」，但**第一次上云时如果有报错，属于预期内**，
按下面「排查」一节逐项对。

## 1. 需要什么

- 一台 Linux 云主机（ubuntu 22.04 / 24.04 都行）。规格：**2 核 2G 起**，1G 会很紧张
  （MySQL 一启动就吃掉大半内存，构建镜像时容易 OOM）。磁盘 20G 起。
- 安全组放行：**22（SSH）** 和 **API_PORT（默认 8000）**。
  如果启用 §6 的反代，那么放行 **80**、把 **8000 撤掉**。
  ⚠️ 不要放行 3306 / 6379 —— 见第 4 节。
- 如果想让聊天功能真的能回复，主机上还要有 Ollama（见第 5 节）。

学生机提示：腾讯云/阿里云都有学生优惠，2C2G 的轻量服务器足够跑这个项目。

## 2. 三步上线

```bash
# ⓪ 先在本地跑一遍编排文件的自洽性校验（不需要 Docker）
python -m unittest tests.test_deploy_manifest

# ① 装 Docker（官方一键脚本）
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER      # 之后重新登录，否则 docker 命令要 sudo

# ② 拉代码
git clone https://github.com/Jia-ben00/ollama-streamlit-chatbot.git
cd ollama-streamlit-chatbot

# ③ 填配置 + 一键部署
cp .env.prod.example .env
vi .env                            # 至少填 MYSQL_ROOT_PASSWORD（openssl rand -hex 24）
                                   # 国内主机再填 PIP_INDEX_URL，见下
bash deploy.sh

# ④ 验收：把整套容器逐项断言一遍（和 CI 里跑的是同一个脚本）
bash .github/scripts/container_smoke.sh
```

**③ 里那行 `PIP_INDEX_URL` 值得单独说一句**，因为它是「几分钟」和「几小时」的差别：
装依赖是构建里最慢的一层，而快慢几乎完全取决于 PyPI 通不通。本机实测（国内网络，
同一台机器同一个 Docker）：官方 PyPI 约 **45 KB/s**，那一层跑了 **3.7 小时**还没完
（pyarrow 一个包 2.6 小时）；换成镜像后同一个包 **106 秒**（427 KB/s），整层几分钟结束。
`deploy.sh` 会在**开始构建之前**提醒一次（这样你还能 Ctrl-C 去改 `.env`），
`container_smoke.sh` 也会打印当前用的是哪个源。

第 ⓪ 步的用处：把「只会在上云第一小时才暴露」的那类问题（`.env` 被拷进镜像、
`host.docker.internal` 在 Linux 上不解析、`.env` 里改了参数却没透传…）提前拦在本地。
这些判断都是静态的，不需要 Docker，所以能在本机跑、也在每次 CI 里跑。

第 ④ 步是这轮补的**验收脚本**，它和 CI 里跑的是同一份代码
（`.github/scripts/container_smoke.sh`），会检查那些「文件看着对、跑起来却不对」的事：

- 镜像里**确实没有** `.env` / `.git` / `tests`（`.dockerignore` 真的生效，而不只是写了规则）
- 容器**确实以非 root 运行**（`Dockerfile` 里的 `USER appuser` 真的生效）
- `mysql` / `redis` **确实没有**把端口映射到宿主机，只有 api 暴露
- 三个依赖探针全为 `true`（`REDIS_URL` 指向服务名、`extra_hosts` 解析通了 Ollama）
- 流式聊天真的按块到达（每条间隔贴合服务端节奏，没有哪一层在攒批）
- emoji 经**容器里的 MySQL** 往返无损（证明 `--character-set-server=utf8mb4` 生效了）

> 收尾的规矩：脚本只在「服务是它自己拉起来的」时候才 `docker compose down`
> **且带 `-v`**。你刚跑完 `deploy.sh` 再跑它，它检测到服务已在运行，就只做断言、
> 不动你的服务，**不会删数据卷**。CI 里则是从头拉起、跑完清干净。

`deploy.sh` 会依次做：前置检查（docker / compose / `.env` / 密码强度 / 启用 `--proxy`
时还要 `API_BIND=127.0.0.1`）→ 提醒构建会走哪个 pip 源 → `git pull`（`--no-pull` 跳过）
→ `docker compose up -d --build` → 轮询 `/health/ready` 最多 180 秒 → 打印依赖状态
和验证命令。任何一步失败都会打印**能直接照抄的排查命令**。

> 这个脚本长期处于「谁也没执行过」的状态 —— CI 直接跑 `container_smoke.sh`，
> 静态校验只把它当**文本**读（查行尾、查字符串）。第一次真跑（替身 `docker`）
> 就抓到一个静态校验永远看不见的洞：`.env` 里少一行 `MYSQL_ROOT_PASSWORD=` 时，
> 它**连一句输出都没有**就退出（`set -e` + `pipefail`；同一段里 `API_PORT` 那处
> 写了 `|| true`，所以只有密码这处会静默退出）。
> 现在它被两处钉着：`tests/test_deploy_script.py`（20 条，走完它的每条分支）
> 和 `container_smoke.sh` 第 5 步（真 Docker 上跑**同一条命令**）。

## 3. 怎么算部署成功

```bash
# 在服务器上
curl -s http://127.0.0.1:8000/health
# 在本机（把 IP 换成你的）
curl -s http://<公网IP>:8000/health
```

期望看到（三个依赖都通）：

```json
{"status":"ok","checks":{"database":true,"redis":true,"ollama":true}}
```

只要 `database` 是 `true`，就说明**部署本身成功了**。`redis` / `ollama` 为 `false`
时服务仍然可用（降级），只是少了缓存 / 少了聊天能力——这是设计如此，不是故障。

- `redis: false` → 检查 compose 里 api 的 `REDIS_URL` 有没有写成服务名 `redis`
  （写成 127.0.0.1 会静默失效：容器里的 127.0.0.1 是它自己）。
- `ollama: false` → 见第 5 节。

## 4. 两个故意的「不暴露」

`docker-compose.yml` 里 **没有**把 mysql(3306) / redis(6379) 映射到宿主机。
原因很实在：

- MySQL 端口暴露到公网，等于把数据库直接交给全世界的扫描器；
- Redis 默认**没有密码**，暴露出去就是历史上一大批「被写 SSH key / 被挖矿」的
  服务器来源（`CONFIG SET dir` + `dbfilename` 直接能写文件）。

容器之间走 compose 内网（服务名 `mysql` / `redis`）互访，根本不需要宿主机端口。
要在服务器上连库调试时：

```bash
docker compose exec mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" chatbot
docker compose exec redis redis-cli
```

## 5. Ollama 的三种接法

`OLLAMA_BASE_URL` 在 `.env` 里配，默认 `http://host.docker.internal:11434`
（容器访问「宿主机上的 Ollama」）。

| 场景 | 配置 | 注意 |
|---|---|---|
| 云主机上也装了 Ollama | `http://host.docker.internal:11434` | 已内置 `extra_hosts` 映射，无需手工配置（见下） |
| Ollama 在另一台机器 | `http://<内网IP>:11434` | 优先走内网，别走公网 |
| 暂时不接 | 保持默认 | 服务照常启动，`/health` 里 `ollama: false`，`/chat` 会返回错误事件 |

关于 `host.docker.internal`：**Linux 上这个名字不会自动解析**——Docker Desktop for
Mac/Windows 才自带它，Linux 原生 Docker 的设计是「显式 opt-in」。所以
`docker-compose.yml` 里给 api 服务加了这段：

```yaml
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

（`host-gateway` 是 Docker 20.10+ 的特殊值，运行时替换为宿主机网关 IP。
这是平台差异，不是配置写错。有 `tests/test_deploy_manifest.py` 守着——
哪天有人把这行删了，CI 会红，而不是等到云主机上报 `Name or service not known`。）

⚠️ **公网暴露 Ollama 是危险的**：它默认无鉴权，任何人拿到地址就能白嫖你的算力，
甚至通过 `/api/pull` 让你磁盘爆掉。要对外开放就先套一层带鉴权的反向代理。

## 6. Nginx 反代（可选，但上线建议开）

直接暴露 8000 端口能用，但加上 Nginx 才能拿到 HTTPS、真实客户端 IP 和一层限流。

**配置不用你手抄**，仓库里就有：`deploy/nginx/templates/default.conf.template`。
它交给 nginx 官方镜像的模板机制渲染——容器启动时会执行
`docker-entrypoint.d/20-envsubst-on-templates.sh`，把模板里的 `${SERVER_NAME}` /
`${API_UPSTREAM}` / `${LISTEN_PORT}` 换成容器环境变量的值，输出到 `/etc/nginx/conf.d/`。
好处是**配置的单一来源在仓库里**：在服务器上手写一份 nginx.conf，下一次 `git pull`
之后两边就漂移了，而且没有任何东西会提醒你。

启用（compose 里 `proxy` 放在 `proxy` profile 下，默认不动）：

```bash
# .env 里填 SERVER_NAME（还没域名就留 `_`，用 IP 也能访问）
docker compose --profile proxy up -d
```

拓扑变成 `公网 → nginx → api:8000 → mysql / redis`，nginx 与 api 走 compose 内网
服务名互访，不需要经过宿主机端口。

> **启用反代后建议把安全组里的 8000 撤掉，只放行 80/443。** 否则别人可以绕过反代
> 直连应用——以后加的限流/鉴权就都绕过去了。
> 想在服务器本机直连调试的话，把 compose 里 api 的 `ports` 改成
> `"127.0.0.1:${API_PORT:-8000}:8000"`：只绑回环，公网到不了，本机照常能查。

### 6.1 怎么确认「流式没被反代攒批」

```bash
# ① 上云之前（本机 / CI）：真 nginx 容器 + 仓库那份模板，不用等服务器
python tests/e2e/nginx_check.py

# ② 上了公网之后，从**外面**（你的笔记本）跑
python tests/e2e/public_check.py --url http://your-domain.com
```

第 ② 行**必须在另一台机器上跑**：在服务器上直连 8000 不经过 Nginx，等于没验。

两个脚本都用裸 socket 记每块的真实到达时刻，判定回复是不是「逐块到达」。
两种失败形态都拦得住：9 块全挤在几毫秒内（中间那层攒批）、
首块直到最后才出现（上游生成完才吐）。

`nginx_check.py` 还会起一个**必须被判成攒批的反例**（开着 `proxy_buffering`，
上游换成 `tests/e2e/plain_sse.py`）——只有正例的话，「通过」什么都证明不了：
一把恒真的尺子也长这样。完整对照表见 `tests/e2e/README.md`。

> ⚠️ 反例要改的是**两个**变量：`proxy_buffering` **和上游**。只改前者、让上游还是
> 那个 chunked 的真应用，反例就永远红不了（chunked 本来就不攒批），
> 而失败形态是看不懂的 `ConnectionResetError`。
> 这一点是**在 CI 上真跑第一次才发现的** —— 开发机没有 Docker，
> 所以这个脚本上云之前从没被执行过。**「本机验不了」不等于「可以先不验」。**

> 一个反直觉的实测结论，值得知道：**nginx 对 chunked 分帧的响应本来就不攒批**，
> 而我们的应用（uvicorn）正是 chunked。所以在当前形态下，配置里那句
> `proxy_buffering off` 和响应头 `X-Accel-Buffering: no` 在客户端**观测不到差别**。
> 留着它们是纵深防御（挡非 chunked 上游、gzip、proxy_cache 这几类形态），
> 不是「靠它救命」。配置注释里也是这么写的，没把它说大。
>
> ⚠️ 一个容易搞反的点：`X-Accel-Buffering` 是应用发给**反代**看的头，
> nginx 读到之后会**消费掉**它（实测：几种配置下客户端都收不到这个头）。
> 所以公网侧的判据只能是**到达时刻**；「响应头里有 X-Accel-Buffering」
> 属于**应用侧**的断言（`tests/test_chat_stream.py`），别把它搬到公网侧来用。

### 6.2 HTTPS

反代这一层有两份模板，都在仓库里，**都真跑过**：

| 配置源（`.env` 的 `NGINX_TEMPLATES_DIR`） | 内容 | 怎么验的 |
|---|---|---|
| `./deploy/nginx/templates`（默认） | 明文，只监听 80 | §6.1：真 nginx 量到达时刻 + 一个必须被红掉的反例 |
| `./deploy/nginx/tls` | 80 只做 301 + ACME 挑战，443 上 TLS | `python tests/e2e/nginx_check.py` 的 C / D 段（见下） |

启用 HTTPS 要动几处，都在 `.env` 里：

```bash
# ① 配置源换成 HTTPS 那份
NGINX_TEMPLATES_DIR=./deploy/nginx/tls
# ② 证书按**固定名字**就位：fullchain.pem + privkey.pem
#    （certbot 的 /etc/letsencrypt/live/<域名>/ 正好就是这两个名字，直接指过去）
TLS_CERT_DIR=/etc/letsencrypt/live/your-domain.com
# ③ 端口（默认就是 80 / 443，一般不用改）
PROXY_HTTP_PORT=80
PROXY_HTTPS_PORT=443

bash deploy.sh --proxy      # up 之前先检查证书在不在，收尾打印 https:// 入口
```

证书缺文件时 `deploy.sh` 会**在启动之前**拒绝：否则 nginx 在容器里当场退出，
而你看到的是一串 nginx 日志加一次白等的构建。

#### 这一层验到了什么程度（边界说清楚）

TLS 长期是本项目**唯一整层没验过**的东西。当初的理由是「ACME 签发要有真域名 + 公网 IP」——
那个理由只覆盖**签发**那一半。「TLS 之后流式还活着吗」「这把尺子在 TLS 下量得出攒批吗」
根本不需要域名：自签一张证书就能量。现在 `tests/e2e/nginx_check.py` 每次运行
（含 CI 容器冒烟第 9 步）都会做这几件事，而且每组正例都配了反例：

| 断言 | 同一次运行里必须红掉的反例 |
|---|---|
| 明文口只回 301（路径保留） | — |
| 经真 TLS 握手后回复仍**逐块到达** | **D**：TLS + `proxy_buffering on` + 靠关连接结束的上游 → 必须判成 BUFFERED |
| HTTPS 上 `/health` 200，`read_sse(use_tls=True)` 真的走了 TLS | **C**：不给信任根（默认信任链）去打自签证书 → 必须 `SSLCertVerificationError` |

两个反例各挡一件事：**C 挡「根本没校验证书」**（谁都能冒充，流式当然也「正常」），
**D 挡「尺子在 TLS 下失效」**（TLS 有记录层分帧，理论上会改变 `recv()` 的切分方式，
不测就只是猜）。顺带一提，`read_sse` 的 `use_tls` 分支在写这段之前**从未被执行过** ——
没被执行过的代码等于没写过。

**还没验的**（必须等真域名，这里不含糊）：

- **ACME 的签发与续期**。模板特意把 `/.well-known/acme-challenge/` 排在跳转之前，
  就是为了续期能工作 —— 但这条只有真签发一次才能证明。**首次部署后手动跑一次
  `certbot renew --dry-run`**：这是整套配置里唯一没法在本机验的环节。
- **HSTS**（`Strict-Transport-Security`）**故意没加**：浏览器一旦记住就很难回退
  （要等 max-age 过期，期间站点直接打不开）。等上面那条续期跑顺了再说。
- **HTTP/2 故意没开**：多一层分帧与多路复用，而 SSE 在 h2 下的到达时刻没量过。
  要开就先在 `nginx_check.py` 里把它量出来再加。

#### 两条可选拓扑

| 做法 | 说明 |
|---|---|
| 在更外层终结 TLS | 用云厂商的负载均衡 / CDN 挂证书，回源到这台机器的 80。证书续期由云厂商管；仓库里那份明文配置直接能用 |
| 本机终结（certbot + 仓库里的 HTTPS 模板） | `certbot certonly --webroot -w /var/www/certbot -d <域名>` 签发，`TLS_CERT_DIR` 指向 `live/<域名>/`，再 `deploy.sh --proxy`。上面那张表的边界同样适用 |

无论走哪条，**效果都能用同一个判据验**：

```bash
# 真实域名：证书由公信 CA 签发，直接跑
python tests/e2e/public_check.py --url https://your-domain.com
# 自签证书（还没域名 / 想先在本地把这一层量一遍）：把信任根换成那张证书
python tests/e2e/public_check.py --url https://127.0.0.1:8443 --ca /path/to/cert.pem --no-ports
```

它不看你用的是什么证书、什么拓扑，只量每块的到达时刻——HTTPS 之后流式是不是还活着，
这才是唯一靠得住的判据。（`--insecure` 会关掉证书校验，只配在本机对自签证书量流式时用。）

## 7. 排查：第一次上云大概率会撞上的

| 现象 | 原因 / 怎么办 |
|---|---|
| 本机 curl 通、公网 curl 不通 | 99% 是**安全组/防火墙**没放行端口。云控制台加入站规则；Ubuntu 还要看 `ufw status` |
| `docker compose` 报 unknown command | 装的是老的 `docker-compose`（带横线）。装 compose 插件，或用 `docker-compose` 命令 |
| `permission denied ... docker.sock` | 用户不在 docker 组。`sudo usermod -aG docker $USER` 后**重新登录**（只重开终端没用） |
| build 到一半 `Killed` | 内存不够（1G 的机器常见）。加 swap 或换 2G 机型：`fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile` |
| **build 卡在装依赖那一层很久**（几十分钟到几小时，没有报错） | 几乎一定是 PyPI 通道问题，不是代码问题。本机实测国内网络下官方源只有 ~45 KB/s，那一层跑了 3.7 小时；换镜像后同一层几分钟。改法：`.env` 里加一行 `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` 再重跑（已完成的层会被缓存复用）。`deploy.sh` 会在构建前提醒，`container_smoke.sh` 会打印当前用的源 |
| `deploy.sh --proxy` 直接拒绝，说 `API_BIND` 不是 `127.0.0.1` | 这是**故意**的，不是 bug：起了反代却让 api 绑 `0.0.0.0`，外面就能绕过反代直连 8000（HTTPS / 限流 / SSE 防缓冲那一层全白配）。按提示把 `.env` 里的 `API_BIND` 改成 `127.0.0.1` 再重跑。只想直连 8000 调试就别加 `--proxy` |
| api 一直重启，日志 `Can't connect to MySQL` | 看 `docker compose logs mysql` 是不是初始化失败；`depends_on: service_healthy` 只保证「健康后再起 api」，若 mysql 自己起不来就要先修 mysql |
| 用 `--proxy` 部署过，`docker compose down` 之后 `proxy` 容器**还在跑、还占着 80/443** | `docker compose down` **不会**停掉属于非激活 profile 的服务。必须带 profile：`docker compose --profile proxy down`（`deploy.sh` 收尾打印的就是这条）。不带的话网络也删不掉（`Network chatbot_default Resource is still in use`），下一次 `up` 直接端口冲突。实测 2026-09-20（compose v5.5.1 / Docker Desktop） |
| `/health` 里 `redis: false` | compose 里 api 的 `REDIS_URL` 必须用服务名 `redis`，不能用 127.0.0.1 |
| `.sh` 报 `bad interpreter: /bin/bash^M` | 文件被转成了 CRLF。仓库里 `.gitattributes` 已声明 `*.sh text eol=lf`，若仍出问题，`sed -i 's/\r$//' deploy.sh` |
| 日志里 `Name or service not known` / `could not resolve host: host.docker.internal` | Linux 不自动解析这个名字，需要 api 的 `extra_hosts`（compose 里已内置）。若你删过那一行，加回来 |
| 在 `.env` 里改了 `TEMPERATURE` / `MAX_TOKENS` 但没生效 | compose 的 `.env` 只做**文件内插值**、不注入容器，api 的 `environment` 里必须显式透传这几个变量 |
| 想确认镜像里没夹带密码 | `docker run --rm <image> cat /app/.env`（正常情况下应报 No such file）；根因是 `.dockerignore` 漏了 `.env` |
| 建出来的表字符集不是 utf8mb4 | 检查 mysql 服务是不是用 `command: --character-set-server=utf8mb4` 起的。写成 `MYSQL_CHARSET` 环境变量**无效**（该变量不被 mysql 官方镜像支持，会被静默忽略） |
| 反代起来之后，回复变成「转圈等到最后出全文」 | 先跑 `python tests/e2e/nginx_check.py`：它自带一个**必须被判成攒批的反例**，能把「尺子坏了」和「真被攒批了」区分开。常见成因是中间多了一层攒批（`proxy_buffering` 被改回去、链路上还有别的代理、或对 `text/event-stream` 开了 gzip）。判据只看到达时刻，见 §6.1 |
| 日志里 `Name or service not known: cd@mysql` 之类的主机名很怪 | 密码里含 `@`。密码是被直接拼进 `DATABASE_URL` 的（`...//root:<密码>@mysql:3306/...`），而 URL 解析器在**第一个** `@` 处切断 userinfo——于是 `ab@cd` 被解析成密码 `ab` + 主机名 `cd@mysql`。实测确认过。改用 `openssl rand -hex 24`；`deploy.sh` 现在会提前拦住含 `@` 的密码 |
| 想重新来一遍 | `docker compose down -v`（**-v 会删数据卷**，只在你确定不要数据时用） |

## 7.5 上线后跑一次验收

**第一步，在服务器上**（验「容器化部署」本身）：

```bash
bash .github/scripts/container_smoke.sh
```

**第二步，从外面验公网入口**（这一步上面那个脚本盖不住——它跑在服务器本机，不经过 Nginx）：

```bash
python tests/e2e/public_check.py --url http://your-domain.com   # 配了 TLS 就换 https
```

四个验收脚本的分工（本机 / 服务器上 / 上云之前的反代层 / 从外面）见 `tests/e2e/README.md`。

和 CI 里跑的是同一份脚本。它会逐项断言「容器化部署真的成立」，包括那些
静态校验证明不了的：镜像里没有凭据、容器非 root、数据库端口没暴露、
三个依赖探针全通、流式没有攒批、容器内 MySQL 真的能存 emoji。
**第 9 步还会用真 nginx 容器再量一次 SSE 的到达时刻**（带一个必须被判成攒批的反例），
也就是反代那一层在上云之前就已经验过了，不靠上线后才发现。

检测到服务已在运行时会**只断言、不收尾**，不会删数据卷。

⚠️ 它会在库里留下几条测试数据（一两个会话和消息），线上跑完可以自行清理。
这是有意的：与其为了「不脏数据」把断言削掉，不如留下几条可辨认的测试记录。

## 8. 回滚

```bash
git log --oneline -5          # 找到上一个能用的提交
git checkout <commit>
docker compose up -d --build
```

数据在 `mysql_data` 卷里，不受代码回滚影响。所以**回滚代码不会丢数据**——
这是把数据库做成 volume 而不是容器内目录的意义。
