# 面试自查：6 个必问点 + 实测证据

这份文档不是「知识点罗列」，而是**每个问题配一条我在这台机器上真跑出来的数字**。
面试官问「你怎么知道」，答案是「我测过，数字是这些」。

运行环境：本机 MySQL 8.0（Windows）、Python 3.12、无 Docker/WSL。
端到端联调用的是临时库 `chatbot_api_e2e`（20 个会话 / 100 条消息）+ 一个假 Ollama
（每 50ms 吐一行 NDJSON，协议与真 Ollama 一致），跑完即 drop，不碰练习库。

---

## 0. 先给结论：这套东西真的跑起来了

| 验证项 | 结果 |
|---|---|
| 单元测试 | `Ran 43 tests ... OK`（31 原有 + 4 会话消息 + 4 缓存 + 4 健康/会话） |
| 端到端冒烟（真 MySQL + 假 Ollama + 真 uvicorn） | 27 项断言全过 |
| 建表 | 用仓库里的 `python -m db.init_db` 在空库建出 **6 张表**，collation 全 `utf8mb4_0900_ai_ci` |
| SSE 流式 | 9 个 chunk，块间隔均匀 **47ms**（服务端设定 50ms），`Content-Type: text/event-stream` |
| emoji 往返 | `表情测试 🚀😀🔥` 经 HTTP → MySQL → HTTP 无损，`@@character_set_connection = utf8mb4` |

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

代码：`cache.py`（+ `tests/test_cache.py` 四条用例覆盖）

- **缓存什么**：会话的最近 20 条消息，也就是发给 Ollama 的对话上下文。key 形如 `chat:conv:{id}:context`。
- **为什么是 Redis**：聊天上下文是热路径。每轮对话都要「系统提示 + 最近 20 条历史」拼一遍，100 轮就是 100 次查询。Redis 是内存 KV，亚毫秒级；MySQL 读一行也要走网络、可能落盘。分工是「MySQL 负责持久化，Redis 负责喂上下文」。
- **什么时候失效**：两条腿。① TTL 1800s——用户半小时不说话自动清掉，Redis 不会无限膨胀，也不用人工清僵尸 key；② 主动失效——每次新消息落库后 `invalidate()`，否则会出现「刚发的消息上下文里还没有」的脏读。
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

## 附：三个「本地跑通 ≠ 生产跑通」的实例

这三条比任何理论都好用，因为都是这轮改造里**真实踩到的**：

| # | 现象 | 根因 | 为什么本地没暴露 |
|---|---|---|---|
| 1 | `Column(Integer, unsigned=True)` 直接抛 `TypeError: Additional arguments should be named <dialectname>_<argument>` | `unsigned` 是 MySQL **方言**参数，不是 SQLAlchemy 通用参数 | 通用类型 vs 方言类型的区别，写的时候不报，只有真建表/编译 DDL 才炸 |
| 2 | CI 里 `test_api` 全挂：`The starlette.testclient module requires the httpx2 package` | 最新版 starlette 的 `TestClient` 换了依赖（`httpx` → `httpx2`） | 本机装的是旧版 fastapi/httpx，能跑；CI 装的是最新版 |
| 3 | `messages.created_at` 上明明有索引，范围查询却 `type=ALL` + `Using filesort` | 3857 行里几乎所有行都满足 `created_at >= '2026-01-01'`，索引选择性≈0，优化器判定「全表 + 内存排序」比「走索引再回表」更便宜 | 这是**数据分布**决定的，不是 SQL 写错。数据量/时间跨度一变，同一个索引就会重新生效 |

第 3 条的完整复核（迁移到 104 行的临时库上同样复现）：

```
消息列表游标分页（WHERE conversation_id=? AND id<? ORDER BY id DESC LIMIT 50）
  type=range  key=ix_messages_conversation_id  rows=5  extra=Using index condition; Backward index scan
时间范围查询（WHERE created_at>=? ORDER BY created_at DESC）
  type=ALL    key=NULL                          rows=104 extra=Using where; Using filesort
```

同一个库，一条查询索引完美生效，另一条完全不生效——**说明"建了索引"和"用上索引"是两件事**。

---

## 一个诚实的边界

`db/models.py` 的 ORM 与练习库 6 张表的**列名、类型、可空性、索引涉及哪些列/是否唯一**是逐列比对过的（连临时库 `create_all` 后对 `information_schema` 抓差异）。但**索引名**是 SQLAlchemy 按 `ix_<表>_<列>` 自动生成的，和练习库里手写的 `idx_msg_conv` / `idx_conv_user` 不一致——功能等价，名字不同。如果面试官问起，这是「命名约定」问题，不影响执行计划。
