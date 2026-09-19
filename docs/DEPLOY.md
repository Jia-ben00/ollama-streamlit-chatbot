# 部署手册（阶段 C：把后端推到公网）

目标：在一台云主机上把 `api + mysql + redis` 三个容器拉起来，让
`http://<公网IP>:8000/health` 能被外面访问到。

## 0. 先说清楚现状（诚实标注）

| 做到哪一步了 | 状态 |
|---|---|
| 镜像与编排文件（`Dockerfile` / `docker-compose.yml`） | ✅ 已写好 |
| 部署脚本（`deploy.sh`） | ✅ 已写好 |
| 编排文件之间的自洽性（compose / Dockerfile / `.dockerignore` / `.env` 模板） | ✅ 已用静态校验守住：`tests/test_deploy_manifest.py`（18 用例）+ 证明这些守卫会失败（7 用例），都在 CI 里 |
| 本机跑通整个后端（真 MySQL + 假 Ollama + 真 uvicorn，27 项断言） | ✅ 已验证 |
| **在真实容器里跑起来** | ❌ **还没验证过** —— 本机没有 WSL、内存 1G，跑不动 Docker |

第 3 行是这轮补的。**「文件写好了」和「文件之间自洽」是两件事**：本机没 Docker，
compose 里写错一个变量名也跑不到，所以改成用静态校验把能查的先查掉——
凡是「只会在上云第一小时暴露」的问题，尽量挪到提交前。

所以这份手册的步骤是「照着做就能上线」，但**第一次上云时如果有报错，属于预期内**，
按下面「排查」一节逐项对。

## 1. 需要什么

- 一台 Linux 云主机（ubuntu 22.04 / 24.04 都行）。规格：**2 核 2G 起**，1G 会很紧张
  （MySQL 一启动就吃掉大半内存，构建镜像时容易 OOM）。磁盘 20G 起。
- 安全组放行：**22（SSH）** 和 **API_PORT（默认 8000）**。
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
vi .env                            # 至少填 MYSQL_ROOT_PASSWORD（openssl rand -base64 24）
bash deploy.sh
```

第 ⓪ 步的用处：把「只会在上云第一小时才暴露」的那类问题（`.env` 被拷进镜像、
`host.docker.internal` 在 Linux 上不解析、`.env` 里改了参数却没透传…）提前拦在本地。
这些判断都是静态的，不需要 Docker，所以能在本机跑、也在每次 CI 里跑。

`deploy.sh` 会依次做：前置检查（docker / compose / .env / 密码强度）→ `git pull`
→ `docker compose up -d --build` → 轮询 `/health/ready` 最多 180 秒 → 打印依赖状态
和验证命令。任何一步失败都会打印**能直接照抄的排查命令**。

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

## 6. Nginx 反代（可选，但建议）

直接暴露 8000 端口能用，但加上 Nginx 才能拿到 HTTPS。最小配置：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;

        # ⚠️ SSE 必须关缓冲，否则 Nginx 会把流攒成一批再发（"流式"变"一次性"）
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;    # 大模型生成慢，别让代理提前掐断
    }
}
```

`proxy_buffering off` 这一条和代码里 `X-Accel-Buffering: no` 是同一件事的两道保险：
应用主动告诉 Nginx 别缓冲，配置层再显式关一次。漏了会怎样见
`docs/interview-notes.md` 第 5 节（那里有一个同类坑的实测数据）。

## 7. 排查：第一次上云大概率会撞上的

| 现象 | 原因 / 怎么办 |
|---|---|
| 本机 curl 通、公网 curl 不通 | 99% 是**安全组/防火墙**没放行端口。云控制台加入站规则；Ubuntu 还要看 `ufw status` |
| `docker compose` 报 unknown command | 装的是老的 `docker-compose`（带横线）。装 compose 插件，或用 `docker-compose` 命令 |
| `permission denied ... docker.sock` | 用户不在 docker 组。`sudo usermod -aG docker $USER` 后**重新登录**（只重开终端没用） |
| build 到一半 `Killed` | 内存不够（1G 的机器常见）。加 swap 或换 2G 机型：`fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile` |
| api 一直重启，日志 `Can't connect to MySQL` | 看 `docker compose logs mysql` 是不是初始化失败；`depends_on: service_healthy` 只保证「健康后再起 api」，若 mysql 自己起不来就要先修 mysql |
| `/health` 里 `redis: false` | compose 里 api 的 `REDIS_URL` 必须用服务名 `redis`，不能用 127.0.0.1 |
| `.sh` 报 `bad interpreter: /bin/bash^M` | 文件被转成了 CRLF。仓库里 `.gitattributes` 已声明 `*.sh text eol=lf`，若仍出问题，`sed -i 's/\r$//' deploy.sh` |
| 日志里 `Name or service not known` / `could not resolve host: host.docker.internal` | Linux 不自动解析这个名字，需要 api 的 `extra_hosts`（compose 里已内置）。若你删过那一行，加回来 |
| 在 `.env` 里改了 `TEMPERATURE` / `MAX_TOKENS` 但没生效 | compose 的 `.env` 只做**文件内插值**、不注入容器，api 的 `environment` 里必须显式透传这几个变量 |
| 想确认镜像里没夹带密码 | `docker run --rm <image> cat /app/.env`（正常情况下应报 No such file）；根因是 `.dockerignore` 漏了 `.env` |
| 建出来的表字符集不是 utf8mb4 | 检查 mysql 服务是不是用 `command: --character-set-server=utf8mb4` 起的。写成 `MYSQL_CHARSET` 环境变量**无效**（该变量不被 mysql 官方镜像支持，会被静默忽略） |
| 想重新来一遍 | `docker compose down -v`（**-v 会删数据卷**，只在你确定不要数据时用） |

## 8. 回滚

```bash
git log --oneline -5          # 找到上一个能用的提交
git checkout <commit>
docker compose up -d --build
```

数据在 `mysql_data` 卷里，不受代码回滚影响。所以**回滚代码不会丢数据**——
这是把数据库做成 volume 而不是容器内目录的意义。
