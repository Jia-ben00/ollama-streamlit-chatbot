# 面试自查：6 个必问点 + 实测证据

这份文档不是「知识点罗列」，而是**每个问题配一条我在这台机器上真跑出来的数字**。
面试官问「你怎么知道」，答案是「我测过，数字是这些」。

运行环境：本机 MySQL 8.0（Windows）、Python 3.13（venv；CI 用 3.11）、
Docker Desktop 4.91 / 引擎 29.8.0（2026-09-19 装上，在此之前本机没有 Docker 也没有 WSL）。
端到端联调用的是临时库 `chatbot_api_e2e`（20 个会话 / 100 条消息）+ 一个假 Ollama
（每 50ms 吐一行 NDJSON，协议与真 Ollama 一致），跑完即 drop，不碰练习库。

---

## 0. 先给结论：这套东西真的跑起来了

| 验证项 | 结果 |
|---|---|
| 单元测试 | `Ran 288 tests ... OK`（领域 31 + HTTP 层 19 + 前端客户端 25 + 会话抽象 33 + 界面 4 + 缓存 4 + 部署清单 20 + 守卫元测试 8 + 上下文与缓存顺序 8 + 流式协议 12 + schema 快照 14 + **容器脚本守卫 10** + 流式判据 10 + 反代配置守卫 14 + **HTTPS 模板守卫 24** + **部署脚本守卫 27** + **反代部署路径守卫 25**） |
| 反向对照（把缺陷种回去） | `tests/e2e/reverse_check.py` 六组全红：chat_stream 7/7、schema 8/8、**container_smoke 4/4**、stream_probe 4/4、**nginx 20/20**、**deploy_script 15/15**（共 58 条种回，全部变红），且每次改完按字节还原（sha256 校验） |
| 端到端冒烟（服务端视角） | `tests/e2e/smoke.py` 27 项断言全过 |
| 端到端冒烟（前端视角） | `tests/e2e/frontend_smoke.py` 30 项断言全过 |
| 建表 | 用仓库里的 `python -m db.init_db` 在空库建出 **6 张表**，collation 全 `utf8mb4_0900_ai_ci` |
| 表字符集是「代码声明」在起作用（受控实验） | 故意建默认字符集为 **latin1** 的库：裸 DDL 建表 → 写 emoji 报 `1366 Incorrect string value`；走 ORM 建表 → 6 张表全 utf8mb4，emoji 往返无损 |
| SSE 流式（服务端到客户端） | 9 个 chunk，块间隔均匀 **47ms**（服务端设定 50ms），`Content-Type: text/event-stream` |
| SSE 流式（前端客户端读到的） | 9 个 chunk，块间隔 **[50, 51, 51, 51, 50, 51, 51, 50] ms** —— 前端侧没有二次缓冲 |
| emoji 往返 | `表情测试 🚀😀🔥` 经 HTTP → MySQL → HTTP 无损，`@@character_set_connection = utf8mb4` |
| **容器化部署（真跑 Docker）** | ✅ CI 每次 push 在 GitHub runner 上真跑整套（含明文 A/B、HTTPS C/C′/D，以及 `deploy.sh --proxy` 那一段；run `35491236413` → **47 PASS / 0 FAIL**，约 2 分 53 秒）；**本机也跑通了**（2026-09-20，Docker Desktop）：`bash .github/scripts/container_smoke.sh` → **47 PASS / 0 FAIL**（8/10 端到端 22 条 + 9/10 反代 14 条 + **10/10 反代部署路径 11 条**）+ 10 项 shell 级 ✓，退出码 0，详见第 9 节 |
| **上机第一条命令（`deploy.sh --proxy`）整条路径** | ✅ 本机真跑过（2026-09-20）：compose 的 `proxy` 服务真的起来了（`profiles: ["proxy"]` 生效）、那颗「先探明文、失败再探 HTTPS」的双模式 healthcheck 在 TLS 下真的通过、明文口 301 跟着跳到得了 HTTPS 200、`public_check.py --ca` 第一次真跑并退出 0；`.env` 全程按字节还原（sha256 一致）。**这条路径在本轮之前谁也没执行过** —— 而它正是云主机上的第一条命令 |
| 容器里三个依赖探针 | `checks={"database":true,"redis":true,"ollama":true}` —— 分别证明「compose 服务名解析」「`REDIS_URL` 指向服务名」「`extra_hosts`/`host-gateway`」三处配置**真的生效**，而不只是写在文件里 |
| 忽略规则与权限真的生效 | 容器内 `ls` 确认 `/app/.env`、`/app/.git`、`/app/tests`、`/app/app.py` 都不存在；容器内 `uid=1000`（非 root）；`mysql:{"3306/tcp":null}`、`redis:{"6379/tcp":null}`、`api: HostPort 8000` |
| **HTTPS（TLS 握手 + 流式 + 尺子的反例）** | ✅ 本机验过：自签证书（SAN 含 `IP:127.0.0.1`）真起 TLS，`nginx_check.py` 的 C/C′/D 三段 —— 明文口 301 保留路径、真 TLS 下 `9 块 / 跨度 0.404s` 仍是 INCREMENTAL、默认信任链打自签证书必须 `SSLCertVerificationError`（证明证书校验真开着）、TLS 下开 buffering 必须被判 BUFFERED |
| 公网访问（云主机 / 安全组 / **ACME 签发与续期**） | ❌ 未验证 —— 缺一台能跑 Docker Compose 的云主机；HTTPS 里「签发与续期」那一半需要真域名，只能上云才验得了，见最后一节 |

---

## 1. 连接池参数怎么定？为什么是这几个数？

**一句话**：`pool_size=5`（稳态并发）+ `max_overflow=10`（兜突发）+ `pool_recycle=3600`（抢在 MySQL `wait_timeout` 前换连接）+ `pool_pre_ping=True`（取连接前先验活）。

代码：`db/session.py`

- 为什么需要池：MySQL 建一条连接要走 TCP 握手 + 认证 + 服务端线程分配，成本几十到上百毫秒。每个请求新建一条 = 连接风暴。
- 为什么不是越大越好：每条常驻连接在 MySQL 侧占一个线程（`threads_connected`）。开 100 个空转连接，换不来吞吐，只换来服务端内存。
- `pool_recycle=3600` 解决的是**具体的报错**：MySQL 默认 `wait_timeout=28800`（8 小时），空闲超时会被服务端单方面断开，应用下次拿到这条"已死"连接就报 `MySQL server has gone away`。让 SQLAlchemy 每小时主动换掉，就不会撞上。
- `pool_pre_ping` 是第二道保险：代价一次极轻的 ping，收益是"偶发第一次请求报错"直接消失。
- 被追问「怎么知道该设多大」→ 压测 + 监控 `Threads_connected`，不是拍脑袋。这个项目并发极低，5 是够用且不浪费的数。

**lifespan 关停时 `engine.dispose()`**（`api/main.py`）：把池里连接还给 MySQL，避免进程退出后留下半开连接等到 `wait_timeout` 才被回收。

---

## 2. 会话列表接口为什么特殊处理？——N+1 的实测数字

**一句话**：naive 写法 23 条 SQL，显式 LEFT JOIN + COUNT 是 1 条。

代码：`api/routers/conversations.py` 的 `list_conversations`

我用 SQLAlchemy 的 `before_cursor_execute` 事件钩子数了真实发出的 SQL 条数（同一份数据、同一个连接，只换写法）：

```
A. 接口实现 list_conversations（返回 22 个会话）发 SQL 条数 = 1
B. naive 实现（22 个会话逐个 len(c.messages)）发 SQL 条数 = 23
   结论：N+1 —— 1 条查会话 + 22 条查消息 = 23 条
```

对应执行计划（`EXPLAIN`，真实库）：

```
会话列表（LEFT JOIN messages + GROUP BY）
  table=c  type=ref  key=ix_conversations_user_id  rows=22   extra=Using index
  table=m  type=ref  key=ix_messages_conversation_id rows=104 extra=Using index
```

两张表都是 `Using index`——也就是说这条查询**全程在索引里完成，没有回表**。原因：
- 按 `user_id` 过滤会话，走 `conversations.user_id` 索引；
- 数消息只用到 `messages.conversation_id`，而这个索引本身就带了这一列，`COUNT` 不需要读行数据，是覆盖索引。

被追问「为什么不用 `relationship` 懒加载」→ 那是「ORM 便利性 vs 查询性能」的取舍：关系导航适合单对象场景，热路径（列表接口）必须显式 JOIN，否则就是 N+1。

`MessageCreate`/`MessageOut` 这类 schema 与 ORM 分离（`api/schemas.py`）也是同一类问题：ORM 是「数据库的形状」，API 是「接口的形状」，直接返回 ORM 对象会泄漏内部字段，也把「改表」和「改接口」耦合起来。

---

## 3. Redis 缓存三问：缓存什么 / 为什么是它 / 什么时候失效

代码：`cache.py`（+ `tests/test_cache.py` 四条用例覆盖）；读写的**顺序**（先读历史还是先落库、末尾 set 还是 delete）在 `api/routers/chat.py`，由 `tests/test_chat_context.py` 八条用例钉住

- **缓存什么**：会话的最近 20 条消息，也就是发给 Ollama 的对话上下文。key 形如 `chat:conv:{id}:context`。
- **为什么是 Redis**：聊天上下文是热路径。每轮对话都要「系统提示 + 最近 20 条历史」拼一遍，100 轮就是 100 次查询。Redis 是内存 KV，亚毫秒级；MySQL 读一行也要走网络、可能落盘。分工是「MySQL 负责持久化，Redis 负责喂上下文」。
- **什么时候失效**：两条腿。① TTL 1800s——用户半小时不说话自动清掉，Redis 不会无限膨胀，也不用人工清僵尸 key；② 每轮结束时把新消息**并入上下文写回（write-through）**，不是删掉缓存。

  **这里踩过一个坑，也是这份笔记里最值得讲下来的一处。** 最初的实现是「落库后 `invalidate()`」。但 `get_context` 在每轮**开头**、`invalidate` 在每轮**末尾**，于是每次请求进来时缓存必然是空的——读缓存永远 MISS，写进去的值从没被任何一次读到过。**命中率恒为 0，Redis 这层在功能上等于没接**（`setex` 白做一次，还多一次 `DEL` 往返）。这不是「命中率低」，是**缓存压根没接上**，只是让代码看起来有缓存。

  它还是第二个缺陷的成因：`_build_context_messages` 在**落库之后**才查历史，查出来的最近 20 条里已经含了本次输入，调用方再 `append` 一次，模型就看到两遍同一句话（`CONTEXT_LIMIT=20` 实际只剩 10 句有效历史）。而因为命中率恒 0，MISS 率正好 100%，所以这个重复是**每轮必然发生**，不是偶发。两条缺陷同一个根因：**读写相对落库的位置**。

  **为什么「删掉 `invalidate`」是错的修法**：不删确实能命中，但缓存里存的是第一次 MISS 那一刻的历史，之后再不更新——命中的请求反而会**丢掉本轮之前的全部对话**，把「不命中」的缺陷换成「命中但内容错」的缺陷。正确修法是写穿，而且写回的是**本轮用到的完整上下文 + 本轮回复**再截断（`(request_messages + [reply])[-CONTEXT_LIMIT:]`），不是只塞本轮两条——只塞两条同样会丢掉更早的历史。

  **这个坑躲过了当时全部 143 个单测。** 原因不是测试写得差，是**替身的形态不对**：`tests/test_api.py` 用 `_FakeQuery` 这种**无状态**替身，查什么返回什么由测试预先写好，它看得见「查了什么」，看不见「先落库还是先查」——而这两条缺陷恰恰都是顺序问题。换成**有状态**假 Session（`add()` + `commit()` 过的消息，后续 `query()` 真能查到）后，原始代码 8 个用例挂 7 个（5 failures + 2 errors），修复版 8 全绿；行为复现脚本实测命中率 **0% → 67%**。67% 而不是 100% 是对的：首轮没有历史可命中，冷启动必然 MISS——**如果看到 100%，反而说明缓冲区里有脏数据。**
- **加分点（挂了怎么办）**：`ConversationCache.__init__` 里 `ping()` 失败就把自己降级成空实现，四个方法全部安全空转。**缓存是加速项，不是正确性依赖**。

这条不是空话，是端到端跑出来的：本次联调故意让 Redis 端口指向一个没人监听的端口，`/health` 返回

```json
{"status":"degraded","checks":{"database":true,"redis":false,"ollama":true}}
```

**数据库和 Ollama 正常、Redis 掉了 → 服务整体降级但不崩**，会话列表仍返回 200 和 22 个会话。这就是 `/health` 为什么要三个探针分开报，而不是一个总开关：出问题时一眼看出是谁挂了。

---

## 4. docker compose 的启动顺序：为什么 healthcheck 和 depends_on 必须配着用

**一句话**：`depends_on` 保证的是**启动顺序**，不是**就绪顺序**。

`docker-compose.yml` 里：

```yaml
mysql:
  healthcheck:
    test: ["CMD", "mysqladmin", "ping", "-h", "localhost", "-u", "root", "-p${MYSQL_ROOT_PASSWORD}"]
    interval: 5s
    retries: 10
    start_period: 20s

api:
  depends_on:
    mysql:
      condition: service_healthy
```

- 只写 `depends_on: [mysql]`，compose 只保证「mysql 容器先 start」，**不保证它已经能接受连接**。MySQL 启动后还要初始化数据目录、建表空间，这几秒到几十秒里 `api` 起来的第一次连接必然失败。
- `condition: service_healthy` 才是「等它真的就绪」。`mysqladmin ping` 返回 0 是 MySQL 真正开始接受连接的信号。
- `start_period=20s` 给第一次初始化留时间，这期间探测失败不算失败，避免冷启动误判。

被追问「不用 compose 时怎么办」→ 应用侧得有重试（本项目 `pool_pre_ping` + 连接池天然带一点容错），或者用 k8s 的 `initContainer` / `readinessProbe`，思路完全一样：**依赖就绪 ≠ 依赖启动**。

> ⚠️ 这一段我诚实标注：本机没有 WSL、内存 1G，**Docker 只写了文件、还没在真实容器里跑过**。这是方案第 4–5 周要去云上验证的事，`docs/DEPLOY.md` 里有步骤。面试被问到就照实说「文件已就绪，本机跑不了 Docker」，不要吹成"已验证"。

---

## 5. SSE 流式怎么实现？——顺带踩到一个「攒批」的坑

**一句话**：`StreamingResponse` 包一个同步生成器，边收 Ollama 的流边 `yield`，流结束后才落 assistant 消息。

代码：`api/routers/chat.py`

- 为什么不能直接 `return`：模型逐 token 生成，等全部生成完再返回，用户盯着空白页等十几秒。透传 Ollama 的流，用户立刻能看到字往外蹦。
- 为什么落库和流式是两件事：流没结束时 assistant 消息不完整，所以「给用户看」边流边给（`yield`），「持久化」放在流结束后一次性做（带 `latency_ms`）。
- 边界情况要说清：流中途断了（客户端断开 / Ollama 报错），assistant 消息可能没落库，生产上要补偿（超时落截断消息），本项目先不做——**主动说出自己没做的部分，比假装完整更可信**。

### 踩到的坑：`requests.iter_lines()` 默认会把流「攒成批」

现象：假 Ollama 每 50ms 推一块，通过 API 却每 **200ms** 才收到一批，首块延迟 172ms。

排查过程（关键是别急着改代码，先把"谁在攒批"定位出来）：
1. 先用裸 socket 读 API 的 SSE——**还是分批**，说明不是 uvicorn 或客户端的事；
2. 再绕过 API，直接打假 Ollama，只改读取粒度，看数字：

```
chunk_size= 512 | 首块 0.156s | 总 0.453s | 间隔 [0.0, 0.0, 0.203, 0.0, 0.0, 0.0, 0.094, 0.0, 0.0]
chunk_size= 256 | 首块 0.078s | 总 0.484s | 间隔 [0.109, 0.0, 0.094, 0.0, 0.109, 0.0, 0.094, 0.0, 0.0]
chunk_size= 128 | 首块 0.063s | 总 0.469s | 间隔 [0.047, 0.062, 0.047, 0.047, 0.047, 0.047, 0.062, 0.047, 0.0]
chunk_size=  64 | 首块 0.062s | 总 0.469s | 间隔 [0.047, 0.063, 0.047, 0.047, 0.046, 0.047, 0.063, 0.047, 0.0]
chunk_size=   1 | 首块 0.000s | 总 0.453s | 间隔 [0.063, 0.046, 0.047, 0.047, 0.047, 0.063, 0.046, 0.047, 0.047]
```

剂量-反应关系非常干净：粒度 512 → 200ms 一批；256 → 100ms 一批；**≤128 就恢复成每块了**；粒度降到 1，首块延迟归零，而**总耗时完全不变**（453ms）。

根因：`read(n)` 的语义是「攒够 n 字节再返回」，而 `iter_lines` 默认 `chunk_size=512`。一行 NDJSON 只有一百多字节，于是要攒 3 行才交出来一次。**服务端一直在推，是客户端在攒。**

修复：`src/ollama_client.py` 里显式 `iter_lines(chunk_size=1, decode_unicode=True)`（常量 `STREAM_READ_CHUNK`）。

修复后同一个端到端测试：

| | 首块 | 块到达间隔 |
|---|---|---|
| 修复前 | 0.172s | `[0, 0, 203, 0, 0, 0, 110, 0]` ms（三块一批） |
| 修复后 | 0.125s | `[47, 47, 47, 63, 46, 47, 47, 63]` ms（逐块） |

**这个坑对面试的价值**：它不是"SSE 理论上会怎样"，而是"接 SSE 的客户端如果用带缓冲的 HTTP 客户端，会看到假的卡顿"。同类问题在前端 `fetch`、Nginx `proxy_buffering` 上都会重现（所以代码里还加了 `X-Accel-Buffering: no` 响应头，防止 Nginx 代理层再攒一次）。

---

## 6. 为什么是 utf8mb4 而不是 utf8？

**一句话**：MySQL 的 `utf8` 是阉割版，每字符最多 3 字节，存不下 emoji；`utf8mb4` 才是真 UTF-8（最多 4 字节）。

- MySQL 的 `utf8`（别名 `utf8mb3`）最多 3 字节 → 4 字节字符（emoji、部分生僻字）直接插不进去，报 `Incorrect string value`。
- 落到三层都要对：**连接串** `?charset=utf8mb4`（`db/session.py`）、**表/库** `utf8mb4_0900_ai_ci`、**容器** `MYSQL_CHARSET/MYSQL_COLLATION`（`docker-compose.yml`）。任何一层掉了都会在中途炸。
- 只改连接不改表也不行——很多人在这栽过：连接是 utf8mb4，表还是 utf8，照样插不进去。

**实测**（端到端跑出来的）：

- 通过 API 发 `表情测试 🚀😀🔥`，走 SSE 落库，再读回来完全一致；
- 直连 MySQL 查那一行，值就是 `表情测试 🚀😀🔥`；
- `SELECT @@character_set_connection, @@collation_connection` → `utf8mb4 / utf8mb4_0900_ai_ci`；
- 临时库 6 张表的 `table_collation` 全部 `utf8mb4_0900_ai_ci`。

顺带一个索引里的细节（面试官如果追问索引长度会用到）：`utf8mb4` 下每字符最多占 4 字节，所以 `varchar(200)` 的索引键最长 800 字节，接近 InnoDB 单列索引 767 字节（老格式）/ 3072 字节（`DYNAMIC` 行格式）的边界——长字段建索引要考虑这个，必要时用前缀索引。

---

## 附：九个「本地跑通 ≠ 生产跑通」的实例

这九条比任何理论都好用，因为都是这轮改造里**真实踩到的**。前三条在代码/数据库层，
第 4–7 条在**部署清单**层，最后两条在**验证手段本身**——它们的共同点是
「本机根本跑不到那段代码」：

| # | 现象 | 根因 | 为什么本地没暴露 |
|---|---|---|---|
| 1 | `Column(Integer, unsigned=True)` 直接抛 `TypeError: Additional arguments should be named <dialectname>_<argument>` | `unsigned` 是 MySQL **方言**参数，不是 SQLAlchemy 通用参数 | 通用类型 vs 方言类型的区别，写的时候不报，只有真建表/编译 DDL 才炸 |
| 2 | CI 里 `test_api` 全挂：`The starlette.testclient module requires the httpx2 package` | 最新版 starlette 的 `TestClient` 换了依赖（`httpx` → `httpx2`） | 本机装的是旧版 fastapi/httpx，能跑；CI 装的是最新版 |
| 3 | `messages.created_at` 上明明有索引，范围查询却 `type=ALL` + `Using filesort` | 3857 行里几乎所有行都满足 `created_at >= '2026-01-01'`，索引选择性≈0，优化器判定「全表 + 内存排序」比「走索引再回表」更便宜 | 这是**数据分布**决定的，不是 SQL 写错。数据量/时间跨度一变，同一个索引就会重新生效 |
| 4 | `.dockerignore` 缺了，`COPY . .` 把 `.env`（含 MySQL root 密码）一起拷进镜像 | 构建上下文默认包含 `.env`，Docker 只排除 `.dockerignore` 里写的东西。而 `deploy.sh` 要求 build **之前**先 `cp .env.prod.example .env`，所以它一定存在 | 直觉是「`.env` 不进 git 就不会进镜像」——但 `.gitignore` 和 `.dockerignore` 是**两套互不相干的排除规则** |
| 5 | 容器里报 `could not resolve host: host.docker.internal` | Linux 原生 Docker 不提供这个别名（那是 Docker Desktop for Mac/Windows 的行为），要显式写 `extra_hosts: host.docker.internal:host-gateway`（Docker 20.10+） | 本机是 Windows：若用 Docker Desktop 试，**这条永远复现不出来**，只有 Linux 云主机上才炸 |
| 6 | 在 `.env` 里改了 `TEMPERATURE`，容器读到的还是默认值 | compose 的 `.env` 只用于**文件内变量插值**，不会注入容器环境；变量必须在 service 的 `environment` 里显式透传 | 本地直连模式读的是同一个 `.env`、改了就生效 —— 两套加载机制长得很像，行为不同 |
| 7 | 用 `MYSQL_CHARSET` 环境变量配字符集，实际没生效 | mysql **官方**镜像不支持该变量（那是 mariadb 镜像的）；官方机制是把 mysqld 参数放在命令行末尾透传 | 不报错，且 MySQL 8 默认字符集恰好就是 utf8mb4 —— 结果「碰巧正确」，把无效配置掩盖了 |
| 8 | 容器连不上宿主机的假 Ollama，报 `Connection refused`（宿主机自己 curl 却通） | 假 Ollama 绑的是 `127.0.0.1`，而容器里的 `host.docker.internal` 解析到的是宿主机在 docker 网桥上的地址（172.17.0.1），不是回环地址 | 本机联调时「假 Ollama 和 API 都在宿主机上」，回环地址够用；一到容器就分属两个网络栈 |
| 9 | 一条容器内检查打了 ✓，实际它根本没执行成功 | `X="$(cmd \| tail -1)"` 的退出码来自管道最后那个 `tail`，`cmd` 失败时 `X` 是空串，「拿不到结果」被当成了「结果正常」 | 这不是环境差异，而是**检查写法**的缺陷——它静默失效，且失效时还在报喜 |

第 4 条最值得讲：**它是一条安全边界，不是省空间技巧。** 镜像一旦构建出来，
里面的东西就收不回来了（`docker history`、导出镜像层都能读出）。把 `.dockerignore`
当成「排除清单」，本质是在回答「哪些文件允许进入产物」。

第 5–7 条的共同结构是：**同一个配置项，在两个平台上语义不同，或者看起来生效了其实没有。**
这类问题没有单元测试能覆盖，只能用「静态校验清单」把它们挡住（见下一节）。

第 8–9 条又不同一层：它们是**验证手段本身**的缺陷。第 8 条是「本机和容器分属两个网络栈」，
第 9 条更值得讲——**一个失效时还在报 ✓ 的检查**。前七条是「我不知道那里有问题」，
后两条是「我以为我验过了，其实没验到」。后者的危害更大：它会让人停止怀疑。

第 3 条的完整复核（迁移到 104 行的临时库上同样复现）：

```
消息列表游标分页（WHERE conversation_id=? AND id<? ORDER BY id DESC LIMIT 50）
  type=range  key=ix_messages_conversation_id  rows=5  extra=Using index condition; Backward index scan
时间范围查询（WHERE created_at>=? ORDER BY created_at DESC）
  type=ALL    key=NULL                          rows=104 extra=Using where; Using filesort
```

同一个库，一条查询索引完美生效，另一条完全不生效——**说明"建了索引"和"用上索引"是两件事**。

---

## 附 2：怎么防止「测试变成装饰」——给守卫做反向对照

上面那些部署清单问题，我用 `tests/test_deploy_manifest.py` 静态守住（20 条断言，进 CI）。
但这里有个更隐蔽的风险：**断言写松了、或者写成恒真条件，它照样全绿，问题照旧上线。**

所以再加一层元测试 `tests/test_deploy_manifest_guards.py`：把每个要防的缺陷**种回去**，
确认对应用例真的会失败。做法不需要 Docker —— 那个模块是纯文件解析、路径来自模块级常量，
把常量指到「被改坏的副本」上就能验证。

实测 8/8 全部生效：

| 种回的缺陷 | 应该报错的守卫 |
|---|---|
| api 少透传 `REDIS_URL` | `test_api_receives_every_env_var_the_code_reads` ✅ |
| 删掉 `extra_hosts` | `test_host_docker_internal_requires_extra_hosts` ✅ |
| 透传一个没人读的变量 | `test_api_env_has_no_dead_entries` ✅ |
| 字符集写成 `MYSQL_CHARSET` | `test_mysql_charset_is_set_via_mysqld_command` ✅ |
| 把 3306 映射到宿主机 | `test_only_api_publishes_ports` ✅ |
| `.dockerignore` 不再排除 `.env` | `test_credentials_and_vcs_are_excluded` ✅ |
| 排除掉 Dockerfile 要 COPY 的文件 | `test_copy_sources_are_not_dockerignored` ✅ |
| 去掉 `.gitattributes` 的 `eol=lf` | `test_gitattributes_forces_lf_for_container_files` ✅ |

一句话：**「测试通过」本身也需要被验证。** 尤其在用工具生成测试的时候，
一个恒真的断言看起来和真断言一模一样。

还有一条设计上的关键：那份「容器必须拿到哪些环境变量」的清单
**不是手写的，而是从源码里反推的**——扫描 `api/ db/ src/ cache.py` 里所有
`os.getenv("X")` 写法。手写清单会随代码漂移然后失效；反推的清单在加新配置的那一刻就会报红。
（同一条思路还有一个副产品：它能反向查出「配了但没人读」的死配置。）

---

## 7. 前端接上后端：同一个坑，在客户端又踩了一次

**一句话**：服务端读 Ollama 的流会攒批，前端读自己后端的流**同样会**——
`requests.iter_lines()` 默认 `chunk_size=512` 这个坑，在链路两端各存在一次。

- 第一次（服务端）：`src/ollama_client.py` 读 Ollama 的 NDJSON；
- 第二次（前端）：`src/api_client.py` 读自家后端推的 SSE。

两处都必须显式传 `chunk_size=STREAM_READ_CHUNK`（= 1），而且**用的是同一个常量**——
免得哪天改了一边、忘了另一边。

实测（`tests/e2e/frontend_smoke.py`，假 Ollama 每 50ms 推一块）：

```
块到达间隔（毫秒）：[50, 51, 51, 51, 50, 51, 51, 50]
[OK]   最大块间隔 < 200ms（没有攒批） max=51ms
```

**为什么这个坑值得单独讲**：它有三个特点——① 有「换一层就重现一次」的性质；
② 症状隐蔽（功能完全正常，只是变慢）；③ 每一层的默认行为都是**缓冲**：

| 层 | 默认行为 | 解药 |
|---|---|---|
| 服务端读 Ollama | `iter_lines(chunk_size=512)`：攒够 512 字节才返回一次 | 显式 `chunk_size=1` |
| 前端读后端 | 同一段代码逻辑，同样攒批 | 同上（复用同一个常量） |
| Nginx 反代 | 默认 `proxy_buffering on` —— 但对 **chunked 上游**实测本就不攒批（明文 `0.405s` / TLS `0.404s`，与直连无异） | 仍显式写 `proxy_buffering off;`，属**纵深防御**：换成 close-delimited 上游时这层真会攒批（反例 D 实测 `BUFFERED`）。注意应用侧的 `X-Accel-Buffering` 头**客户端看不到**（被 nginx 消费掉），所以这层只能靠到达时刻判 |
| 浏览器 / 中间件 | 一般不留缓冲，但压缩中间件、Service Worker 可能引入 | 视情况而定 |

**所以「我做了 SSE」不等于「用户真的看到了流式输出」。** 端到端量一次到达间隔，才算数。

### 为什么必须单独写一个「前端视角」的 e2e

`smoke.py` 用裸 `requests` 打得完美，不代表界面就是对的：

- `smoke.py` 读流的姿势和前端**不一样**（它自己控制 `chunk_size`；前端走 `ChatAPIClient`）；
- 界面上还有一层**只有界面才需要**的转换：模型名 ↔ `model_id`、会话下拉、
  后端模式下系统提示词不可改……这些接口测试完全覆盖不到。

于是有了第二个脚本，用**前端真正会用的那个客户端**再跑一遍 30 项断言。
一句话：**接口正确 ≠ 界面正确，中间那一层必须单独验。**

---

## 8. 接口设计漏掉的不是功能，是「调用方拿到入口的那一步」

后端骨架先做完时，接口全是按「已经知道 user_id / model_id 的调用方」设计的：
`POST /conversations` 要 user_id 和 model_id，`POST /chat` 要 conversation_id。
这些 id 从哪来？当时的答案是「调用方自己知道」，所以 e2e 脚本里写的就是 `user_id=1, model_id=1`。

等真的把 Streamlit 接上去，第一个动作就卡住了：**前端不知道这两个 id，它连数据库都连不上**
（这正是后端化的目的）。所以必然要先问服务端「有哪些模型、哪些用户可选」，
于是补出 `GET /models` 和 `GET /users`。

**这类缺口很难在设计阶段发现**，因为它不是「少写了一个功能」，而是
「少了一条让调用方进来的路」。一个可操作的自检办法：**把接口文档交给一个没参与开发的人去写客户端**——
只要他问出「那我怎么知道该传哪个 id」，就是漏了一个入口。

两个附带判断，面试可以讲：

- **为什么这个用户接口该存在、但真实系统里不该有**：真实系统里「我是谁」来自凭据
  （JWT / Session），让客户端从用户列表里挑一个是越权。这里保留它，是因为鉴权不在本次范围，
  而前端确实需要一个 user_id 才能建会话——**所以它是一条被明确标注的技术债，不是设计疏漏**。
  代码里写清楚它为什么存在、真实做法是什么，比假装它是正确设计更可信。
- **为什么 `UserOut` 里没有 `email`**：`users` 表有这一列，不代表接口该发出去。
  邮箱是 PII，一个「选当前用户」的下拉框不需要它。响应模型在这里起的是**白名单**作用——
  这条有单元测试守着（`test_users_does_not_leak_email`）：一旦有人图省事改成直接
  `return db.query(User).all()`，测试立刻变红。

顺带一个「PATCH 语义」的点：**换模型走的是 `PATCH /conversations/{id}`，
而不是把模型塞进 `POST /chat` 的请求参数。** 因为会话的模型应该是稳定的——
同一段对话前半段用 A 模型、后半段用 B 模型，上下文会不连贯。
代价是「用户想换模型」必须有地方改这个绑定，那就是这个 PATCH。
而 PATCH 用「可选字段」表达「只改我说的」：前端不必先 GET 再全量回填，
也就不会在两个标签页之间发生「读—改—写」把别人的修改覆盖掉（丢失更新）。

---

## 9. 本机没有 Docker 时怎么证明「容器化部署真的成立」——以及装上 Docker 之后它又暴露了什么

这是这个项目此前最弱的一点，也是面试官最容易一句话问到的地方：
**「你写了 Dockerfile 和 compose，跑过吗？」** 当时只能回答「没有，本机没 Docker 也没 WSL」。

办法是换个地方跑：**GitHub 的 runner 自带 Docker**。于是
`.github/workflows/container-smoke.yml` 每次 push 都把整套 compose 真拉起来，
跑 `.github/scripts/container_smoke.sh`。同一个脚本在云主机上就是**上线验收脚本**。

> 2026-09-19 本机装上了 Docker Desktop（4.91 / 引擎 29.8.0 / compose v5.5.1），
> 这条链在本机也能跑通了。2026-09-20 补上 HTTPS、再补上第 10 步之后本机复跑：
> `bash .github/scripts/container_smoke.sh` → **47 PASS / 0 FAIL，退出码 0**
> （8/10 端到端 22 条 + 9/10 反代验收 14 条 + 10/10 反代部署路径 11 条）。
> 第 9 步在**两条通道**上各量一次到达时刻：
> 明文 `9 块 / 跨度 0.405s`、真 TLS `9 块 / 跨度 0.404s`，两者都是 `INCREMENTAL`；
> 同一把尺子在**两个反例**上（明文关掉 `proxy_buffering off`、TLS 下同样关掉）都判成 `BUFFERED`。
> 第 10 步则是**真跑 `deploy.sh --proxy`**（见下面「第二条『谁也没执行过』的路径」）。
>
> 但「本机能跑」不是重点。重点是：**装上之后第一次真跑，暴露了三个此前没人看见的问题** ——
> 每一个都不是「配置写错」，而是「检查本身不成立」。

### 装上 Docker 之后第一次真跑暴露的问题

**① `deploy.sh` 从来没被执行过。** 它是文档里的「上机第一步」，但 CI 直接跑
`container_smoke.sh`，静态校验只把它当**文本**读。第一次真跑（替身 `docker`，
20 条用例）就抓到：`.env` 里少一行 `MYSQL_ROOT_PASSWORD=` 时，脚本**连一句输出都没有**
就退出（`set -e` + `pipefail` 让赋值语句直接终止脚本）。现在它在两处被钉住 ——
`tests/test_deploy_script.py` 与 `container_smoke.sh` 第 5 步（真跑同一条命令）。

**② 装依赖那一层能慢 45 倍，而「慢」和「卡住」在屏幕上长得一样。**
同一台机器同一个 Docker，只有 PyPI 源不同：

| pip 源 | 装依赖那一层 | pyarrow（50.1 MB） |
|---|---|---|
| 官方 PyPI | 跑了 **3.7 小时**还没完 | 2.6 小时（约 45 KB/s） |
| 清华镜像 | **293.7 秒**（`#9 DONE 293.7s`） | **106 秒**（427.6 kB/s） |

所以现在 `.env` 有 `PIP_INDEX_URL` 这个开关，而且 `deploy.sh` 会在**构建之前**提醒
（提醒放在构建之后是没有用的：那时只能等），`container_smoke.sh` 会打印当前用的源。
一句会骗人的提醒比没有提醒更糟 —— 第一版只读 `.env`，我 `export PIP_INDEX_URL=镜像`
时 compose 走了镜像、它却在屏幕上说「将走官方源、可能要几小时」，所以现在取值顺序
和 compose 一致（**shell 环境优先于 `.env`**）。

**③ 假 Ollama 起来了，容器却连到了别人的 Ollama。** 这台机器上装着一个真的 Ollama
（占 `127.0.0.1:11434`），而 **Windows 允许 `0.0.0.0:11434` 与它同时绑定**（Linux 会
直接 `EADDRINUSE`）。于是替身正常起来、脚本打印「假 Ollama 已监听」，可容器里的
`host.docker.internal:11434` 经 Docker Desktop 转发出去打到宿主机**回环**，
连到的是那个真 Ollama：

```
/health          → checks={'ollama': true}      真 Ollama 答的 /api/tags
/chat            → 404 {"error":"model 'llama3.2' not found"}   真 Ollama 没这个模型
冒烟脚本的报告    → [FAIL] 流式返回 9 个 chunk | reply=''        看不出是"连错了服务"
```

更糟的是脚本原来那句「就绪探测」只问「端口通不通」—— 端口是通的，只是**通到别人家**。
同一类错误在 Linux 上的表现是：替身 bind 失败，而探测被**别人**应答，脚本照样打印成功。
修法是两条 fail-closed 检查：起替身**之前**先确认端口没被占；起完再核**指纹**
（`/api/tags` 里那条只有替身会返回的记录），并且**在容器里再验一次**
（容器走的路径和宿主机不同）。加上这两条之后，同样的情况下冒烟脚本早停在一条能照着修的报错上。

> 顺带一个纯粹属于开发机的坑，但值得记：装 WSL 之后 `C:\Windows\System32\bash.exe`
> 出现了，而 Windows 的 CreateProcess 搜索顺序把 System32 排在 PATH **之前** ——
> 于是 `subprocess.run(["bash", ...])` 命中的是 WSL 转发器，而 `shutil.which("bash")`
> 拿到的是 Git Bash。**守卫（skipIf）和被测对象用了两个不同的 bash**，
> 3 条容器用例因此变红（报错是 `execvpe(/bin/bash) failed`）。改成两处共用同一个
> 解析结果（传绝对路径）后恢复。这不是代码缺陷，是环境变更 —— 但它提醒的是同一件事：
> **「拿到一个 bash」和「拿到那个 bash」不是一回事。**

### 关键认识：静态校验和真跑一遍，证明的是两件事

上一轮补了 19 条部署清单静态校验，它们全是「读文件比对文本」。这类校验能证明
**文件里写了正确的规则**，但证明不了**规则真的生效**：

| 断言 | 静态校验为什么不够 |
|---|---|
| 镜像里 `ls` 不到 `.env` / `.git` / `tests` | `.dockerignore` 里写了 `.env` ≠ 规则生效（行尾变 CRLF，模式带了 `\r` 就匹配不上，排除静默失效） |
| 容器内 uid ≠ 0 | `Dockerfile` 写了 `USER appuser` ≠ 生效 |
| mysql / redis 无 `HostPort` | 配置写对 ≠ 运行时真的没暴露 |
| `/health` 的 `redis: true` | `REDIS_URL` 指向服务名这件事真的生效了（写成 127.0.0.1 不报错，只是永远不缓存） |
| `/health` 的 `ollama: true` | `extra_hosts: host-gateway` 真的解析通了 |
| emoji 经容器内 MySQL 往返无损 | `--character-set-server=utf8mb4` 真的生效了 |

所以两层刻意分工、都要有：静态校验**毫秒级**、每次 push 都跑，负责拦住配置漂移；
容器冒烟**几分钟**，负责证明「跑起来是对的」。把后者并进主 CI 是错的——
主 CI 是那个 badge 的依据，混进要拉三个镜像、等 MySQL 初始化的步骤之后，
badge 的含义就从「代码是对的」变成「代码是对的、而且今天的机器不忙」，
那比没有 badge 更糟：会让人习惯性忽略红灯。**所以它单独一个工作流。**

### 真跑才暴露的三个问题

1. **假 Ollama 必须绑 `0.0.0.0`，不能绑 `127.0.0.1`。**
   容器里的 `host.docker.internal` 解析到的是宿主机在 docker 网桥上的地址
   （如 172.17.0.1），不是回环地址。只绑回环时宿主机自己 curl 得通、容器连不上，
   报错还是 `Connection refused` —— 很容易误判成「服务没起」。
   本机跑（假 Ollama 和 API 都在宿主机上）永远复现不出来。

2. **`X="$(cmd | tail -1)"` 这种检查会静默通过。**
   `$()` 的退出码来自管道最后一个命令（`tail`），`cmd` 失败时 `X` 为空字符串，
   于是「拿不到结果」被当成「结果正常」——检查本身失效了，而它还在打印 ✓。
   改法：让被测命令输出一个固定标记（`LEAKCHECK=CLEAN`），拿不到标记就报错，
   **所有检查 fail-closed**。一个失败时不会失败的检查，比没有检查更危险。

3. **`openssl rand -base64 24` 生成的密码，会不会破坏 `DATABASE_URL`？**
   值得单独说，因为这次**差点修了一个不存在的问题**。原因是：密码被直接拼进
   `mysql+pymysql://root:<密码>@mysql:3306/chatbot`，而 base64 会产生 `/` `+` `=`。
   直觉上该有坑，但实测（`sqlalchemy.engine.make_url` 逐个字符试）结果是：

   ```
   hex 安全         host=mysql  password 原样
   base64 含 /      host=mysql  password 原样
   base64 含 +      host=mysql  password 原样
   base64 含 =      host=mysql  password 原样
   含 @             host=cdefghijklmnop@mysql  password 被截成 'ab'   <<< 只有这个坏
   ```

   只有 `@` 会坏（解析器在**第一个** `@` 处切断 userinfo），而 base64 的字符集里
   **没有** `@`。所以文档里的命令是对的，不需要改；需要改的是「用户自己手填一个
   含 `@` 的密码」这条路径——那会得到一个「找不到主机 `cd@mysql`」的报错，
   几乎不可能联想到是密码字符的问题。于是 `deploy.sh` 补了一条针对 `@` 的快速失败。

   这条的价值不在于改了哪行代码，而在于**先测量再动手**：凭直觉改会把
   「文档推荐了一个坏命令」这个错误结论写进仓库。

### 真跑出来的证据（CI run `35489196611`，1m40s）

```
✓ compose 配置可解析，变量插值正常，extra_hosts 已声明
✓ 假 Ollama 在 0.0.0.0:11434 监听（pid=2439）
✓ 镜像里没有 .env / .git / tests / app.py / sentiment_analysis
✓ 容器内 uid=1000（非 root）
✓ mysql 未向宿主机暴露端口（{"3306/tcp":null,"33060/tcp":null}）
✓ redis 未向宿主机暴露端口（{"6379/tcp":null}）
✓ api 已向宿主机暴露端口（{"8000/tcp":[{"HostIp":"0.0.0.0","HostPort":"8000"},...]}）
✓ 第 3 次探测：已就绪
已灌种子：users=1 models=1

[PASS] 容器能连上 MySQL 容器（compose 服务名解析） | checks={'database': True, 'redis': True, 'ollama': True}
[PASS] 容器能连上 Redis 容器（REDIS_URL 指向服务名）
[PASS] /health/ready 返回 200（三依赖全通才就绪） | HTTP 200 {"status":"ready","checks":{...全 true}}
[PASS] 流式返回 9 个 chunk（容器 -> 宿主机假 Ollama -> 容器） | reply='武汉今天多云，22 度，适合出门。'
[PASS] 每块间隔贴合服务端节奏（容器链路没有攒批） | gaps=[0.05, 0.05, 0.051, 0.05, 0.05, 0.051, 0.05, 0.05]
[PASS] emoji 经「容器内 MySQL」往返无损（表字符集真的是 utf8mb4） | '表情测试 🚀😀🔥'
...
── A：仓库配置 → api（真应用，chunked 分帧）──
[PASS] 经真 nginx：回复仍是逐块到达的 | 9 块，跨度 0.405s
── B（反例）：开 proxy_buffering → 裸 SSE 上游 ──
[PASS] 反例确实被判成攒批（证明这把尺子在这里量得出东西） | 实测判定=BUFFERED
── C：仓库的 HTTPS 配置 → 真 TLS 握手 → api ──
[PASS] 明文入口只做跳转：3xx → https 且路径保留 | 301 → https://127.0.0.1/health
[PASS] 经真 TLS：回复仍是逐块到达的（TLS 没有把它攒起来） | 9 块，跨度 0.402s
[PASS] 反例：默认信任链拒绝自签证书（证明证书校验真的开着） | =SSLCertVerificationError
── D（反例）：TLS + proxy_buffering on → 裸 SSE 上游 ──
[PASS] 反例：TLS 下开着 proxy_buffering 仍被判成攒批（尺子在 TLS 上不瞎） | 实测判定=BUFFERED
全部通过：明文与 HTTPS 两条路径上，流式都没有退化成攒批，且判据在两个反例上确实会红。
```

（本机同一条命令现在是 **47 PASS / 0 FAIL**；CI 上 TLS 段是 `9 块 / 0.402s`、本机是 `0.404s`，
差在噪声里 —— 两边量出来的都是 `INCREMENTAL`。）

第 9 步之后还有第 10 步：**让 `deploy.sh --proxy` 真的把 compose 里那个 `proxy` 服务拉起来**。
它此前谁也没启动过（第 9 步用的是 `nginx_check.py` 自己的容器），而它是云主机上的第一条命令。
真实输出（本机，2026-09-20）：

```text
── 2. 真跑 deploy.sh --no-pull --proxy（HTTPS 形态）──
[PASS] deploy.sh --proxy 退出码 0
[PASS] deploy.sh 识别出 HTTPS 形态（收尾提示为「反代已启用（HTTPS）」）
── 3. proxy 服务与它的双模式 healthcheck ──
[PASS] compose 的 proxy 服务起来了（profiles: ["proxy"] 真的生效） | container=96002def1faf
[PASS] proxy 的 healthcheck 通过（TLS 形态下那条 `||` 分支真的被执行了） | health=healthy
── 4. 入口：明文 301 → HTTPS ──
[WARN] 宿主上另有进程占着入口端口 | 127.0.0.1:80 被别的进程抢答：HTTP 404，Server=(无)
       入口地址取 localhost:80（Server: nginx/1.27.5）
[PASS] 明文 :80 不直接服务，只回 3xx | HTTP 301
[PASS] 跟随 301 之后真的到达 HTTPS 并拿到 200（用自签证书校验） | HTTP 200
── 5. 从宿主机验收入口：public_check.py --ca ──
       证书校验：开（信任根换成 .../proxy_deploy_certs_xxx/fullchain.pem）
[PASS] 回复是逐块到达的（中间那层没攒批） | 9 个 chunk 分布在 0.408s 里，首块 0.023s 到达
[PASS] public_check.py --ca 退出码 0（入口验收全过） | rc=0
[PASS] `.env` 已按字节还原（sha256 一致）
```

那条 `[WARN]` 不是本次部署的问题，恰恰是一个**假信号**的实证：宿主机上 80 端口被别的进程
（本机是 Steam++）占着，而 Windows 允许两个进程**同时**绑 `0.0.0.0:80` —— 它回一个
**没有 `Server` 头**的 404。于是「端口能连上」根本不等于「连到的是我们的服务」；
判据只能是 `Server: nginx/...`，`127.0.0.1` 不行就换 `localhost`（IPv6 回环）。

其中三条最能证明「配置真的生效」而不只是「写在文件里」：

- `redis: True` —— 如果把 `REDIS_URL` 删掉或写成 `127.0.0.1`，容器里的 127.0.0.1 是它自己，
  这一项会变 `False`（而且**不报错**，只是永远不缓存）；
- `ollama: True` —— `extra_hosts: host-gateway` 真的把 `host.docker.internal` 解析通了；
- `emoji 往返无损` —— MySQL 容器接受了 `--character-set-server=utf8mb4`，表真的是 utf8mb4。

### 一个顺手发现的破坏性问题

写文档时发现：这个脚本如果在云主机上被当作「验收脚本」用，它末尾的
`docker compose down -v` 会**删掉 MySQL 数据卷**——刚部署完就把线上库清了。
改成「启动前服务已在跑 → 只断言、不收尾」。这类问题不写「上线后怎么用」这一节
是发现不了的：**只考虑 CI 场景，脚本就是对的。**

### 第二条「谁也没执行过」的路径：`deploy.sh --proxy`

上一轮把 `deploy.sh`（不带 `--proxy`）送进了真跑。但**它旁边还有一条分支谁也没走过**：
`bash deploy.sh --proxy` —— 也就是云主机上的**第一条命令**。

漏掉它的原因很具体，而且每一层单看都「没问题」：

- `container_smoke.sh` 第 5 步刻意**不加** `--proxy`，理由还写在注释里（第 9 步的
  `nginx_check.py` 起的是**它自己的** nginx 容器，不需要 compose 里那个 proxy 服务）；
- `tests/test_deploy_script.py` 用替身 `docker` 验的是**分支逻辑**，不是真容器；
- `tests/test_nginx_tls.py` 验的是**编排文件里的文本**。

于是三件事都没有答案：`profiles: ["proxy"]` 真的生效吗、那颗「先探明文、失败再探 HTTPS」
的双模式 healthcheck 在 TLS 下真的会通过吗、「被收回到容器内网的 8000 + 对外的 443」外面
到底能不能验收。补法是第 10 步（`tests/e2e/proxy_deploy_check.py`）。**第一次真跑就抓到三个真问题**：

| 现象 | 根因 | 修法 |
|---|---|---|
| 宿主上 `/health` 回 404 且 **没有 `Server` 头** | 宿主机 80 端口被**别的进程**抢答（本机是 Steam++）。Windows 允许两个进程**同时**绑 `0.0.0.0:80`，所以「端口能连上」根本不等于「连到的是我们的服务」 | 判据改成 `Server: nginx/...`；`127.0.0.1` 不行就换 `localhost`（IPv6 回环通常只有 Docker 在听），两个都不行才失败。抢答时打 `WARN` 并继续 —— 那不是本次部署的问题 |
| `docker compose down` 之后 `proxy` 容器**还在跑、还占着 80/443**，网络报 `Resource is still in use` | `down` 不碰属于**非激活 profile** 的服务 | `container_smoke.sh` 的收尾与 `deploy.sh` 打印的提示都改成 `docker compose --profile proxy down`，并加了一条守卫钉住「打印出来的那条必须带 profile」 |
| `public_check.py --ca` 在**第一步**就 `SSLError` | `--ca` / `--insecure` 只传给了**流式那一段**，`requests` 的 `/`、`/health` 仍走系统信任链 | 抽出 `configure_session()`，把信任根配到**整个 Session** 上（`--ca` 换根但校验仍开，`--insecure` 才关）。这条分支此前同样从没被执行过 |

> 教训和上一轮同源：**「文件写对了」与「跑得起来」之间隔着一段距离**，只有真跑量得出它有多长。
> 第 10 步真跑出来的数字：`deploy.sh --proxy` 退出码 0 → proxy 容器 `healthy`
> → 明文 301 → 跟随到 HTTPS 200 → `public_check.py --ca` 退出 0，`.env` 按字节还原（sha256 一致）；
> 整套冒烟从 36 项涨到 **47 PASS / 0 FAIL**（新增的 11 项全在第 10 步）。

> 还有一条**假信号**值得单独记：第一次把冒烟和第 10 步与**全量单测并行**跑，
> 第 8 步那条「块间隔贴合服务端节奏」红了 —— `gaps=[0.051, 0.05, 0.052, 0.343, 0.003, 0.001, 0.0, 0.001]`。
> 单独重跑同一份代码，间隔是 `[0.051, 0.05, 0.053, 0.049, 0.05, 0.051, 0.051, 0.049]`，干净通过。
> 结论：**时序类的断言对机器负载敏感** —— 别在同一台机器上一边跑 CPU 密集型测试一边量到达时刻，
> 否则你量到的是自己的负载，不是被测对象。

---

## 10. 把「与练习库对齐」变成常驻守卫 —— 顺带抓到两个 `str()` 陷阱

「6 张表的 ORM 与练习库严格对齐」这句话，原来是**没有证据**的：靠手工跑一次脚本、
连上 MySQL 比一遍，跑完就过去了。之后改列、改类型、改索引，不会有任何东西拦。

现在把它变成 CI 里的常驻断言：把练习库结构导出成 `tests/data/practice_db_schema.json`
签进仓库（导出脚本 `tests/e2e/export_schema_snapshot.py`，只有它需要真 MySQL，
所以放 e2e 不进 CI），守卫 `tests/test_schema_snapshot.py` 纯文件解析，
不需要数据库就能跑。

### 比手工脚本多做的两件事

| 做法 | 为什么 |
|---|---|
| 索引按 **(列组合, 是否唯一)** 比，不按名字 | 库叫 `idx_msg_conv`、ORM 叫 `ix_messages_conversation_id`，**同一个索引两个标签**。按名字比会报 5 处假的「缺索引」 |
| **双向**比，不只查「ORM 有、库没有」 | 原脚本只查一个方向，于是「库里有、ORM 没声明」这类问题永远是隐形的 |

（顺带一个坑：MySQL 把**主键**也报成 `information_schema.STATISTICS` 里的一条索引
（名为 `PRIMARY`），ORM 侧不把主键补进去算，会凭空多出 6 处「库里有、ORM 没有」。）

### 两个 `str()` 陷阱 —— 同一个根因，踩了两次

`str(col.type)` 看着就是「这个列的类型」，但它会骗人两次：

| 写法 | `str()` 打印出来 | 实际（MySQL 方言编译） |
|---|---|---|
| `Enum("a","b")` | `VARCHAR(2)` | `ENUM('a','b')` —— `Enum` 继承自 `String`，所以打印成 VARCHAR |
| `INTEGER(unsigned=True)` | `INTEGER` | `INTEGER UNSIGNED` —— **unsigned 被吞掉了** |

第二个陷阱的后果很典型：比对结果会告诉你「ORM 是有符号的、库是无符号的，
有 **13 处**宽严差异」——而这是**假阳性**，两边本来就都是无符号。
也就是说，**报告里会多出一条不存在的结论**。

所以类型必须走 `col.type.compile(dialect=mysql.dialect())`，不能走 `str()`。
为了不再退回去，`TestNormalizationIsNotBlind` 用**元测试**把这个前提钉住：
先断言 `str()` **确实**不带 `UNSIGNED`、`Enum` **确实**打印成 `VARCHAR`，
再断言归一化函数能读出正确结果。哪天有人为了「简化」把归一化改回 `str()`，
这几条会立刻红——而不是让比对悄悄变得没意义。

> 这和「本地跑通 ≠ 生产跑通」是同一类问题：**尺子本身要不要被校验**。
> 一个量不出东西的尺子，看起来和一个量出「全部一致」的尺子一模一样。

### 还要证明「这个检查真的会报错」

「0 处差异」有两个可能：真的对齐了，或者比对永远返回相等。所以除了元测试，
还加了一个**能力检查**：断言「有符号的 ORM 列 vs 无符号的库列」会被判成**不一致**。

另外按附 2 的方法，把 8 个 schema 缺陷逐个种回去：

```
缺列 / 类型不符 / 枚举取值不符 / 可空性不符 / 缺索引 /
缺字符集声明 / 符号不一致（宽严台账）/ 快照里少一张表
                                          → 8/8 全部变红
```

**跑完无条件把文件按字节还原并校验 sha256** —— 中途那次校验没过（`write_text`
在 Windows 上会把 `\n` 翻成 `\r\n`，往返一趟就改写文件），顺着查下去发现
`api/routers/chat.py` 被写成了 172 行 `\r\r\n`。**自检报了警就必须当回事：
如果当时把它当成「行尾噪音」忽略掉，提交进去的就是一个被改坏的文件。**

---

## 一个诚实的边界

`db/models.py` 的 ORM 与练习库 6 张表的**列名、类型、可空性、索引涉及哪些列/是否唯一**是逐列比对过的——而且现在这条比对已经变成 CI 里的常驻守卫（第 10 节），不再依赖「谁记得去跑一次」。但**索引名**是 SQLAlchemy 按 `ix_<表>_<列>` 自动生成的，和练习库里手写的 `idx_msg_conv` / `idx_conv_user` 不一致——功能等价，名字不同。如果面试官问起，这是「命名约定」问题，不影响执行计划。

**第二条**：界面是用 Streamlit 官方的 `AppTest` **无头**跑通的（首屏渲染、数据源切换、
模块切换、异常捕获），但**没有经过真人在浏览器里的手工验收**——布局好不好看、
滚动顺不顺、长文本换行会不会溢出，这些无头测试看不到。要拿它做演示之前，
自己拿手指头点一遍。

**第三条**：后端模式下有两处**故意保留的能力差异**，不是没做完：

| 能力 | 直连模式 | 后端模式 | 为什么 |
|---|---|---|---|
| 改系统提示词 | ✅ 可改 | ❌ 服务端常量 | 多端共用一套提示词，行为才一致；要做成可配置得先给 `/chat` 加字段 |
| 调 temperature / top_p / max_tokens | ✅ 可调 | ❌ 服务端默认 | 同上 |

界面上这两个控件在后端模式下是**置灰 + 写明原因**的，而不是「看起来能调、调了没反应」。
被问到时照实说：这是当前范围外的事，加一个请求字段就能支持，
只是**在没想清楚「参数该由谁决定」之前，不假装它已经支持**。

**第四条**：容器的真实验证已经补上（见第 9 节，CI 每次 push 真跑整套 compose）；
Nginx 反代与 `proxy_buffering off` 对 SSE 的实际影响，也已经在**本机**用真 nginx 验过
（明文 + 真 TLS 两条通道，各带一个反例，见第 9 节）。但**「在云主机上对公网提供服务」
这一步还没有做过**——缺一台能跑 Docker Compose 的 Linux 主机。CI 的 runner 里没有
公网入口，也没有真实的安全组，所以这几件事它盖不住：

- 云安全组放行（典型故障：本机 curl 通、公网 curl 不通）；
- **HTTPS 的证书签发与续期**（ACME / Let's Encrypt 需要真域名 + 公网可达的
  `/.well-known/acme-challenge/`）—— 本机用自签证书验的是「TLS 之后流式还活着吗、
  尺子在 TLS 上还准不准」，**签发那一半验不了**；
- 真实公网链路上的中转（运营商 / CDN / 企业代理）会不会对 SSE 攒批。

`docs/DEPLOY.md` 里对这几条都写了排查表，但**排查表不是实测**，别把它说成验证过。
