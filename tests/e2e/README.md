# 端到端联调（e2e）

`tests/` 根目录下是**单元测试**（mock 掉 MySQL / Redis / Ollama，秒级跑完，CI 里跑这个）。
这里放的是**端到端**：真 MySQL + 真 HTTP + 真 SSE + 假 Ollama，验证「各层拼起来是通的」。

两者都要有：单元测试保证改代码不破坏局部逻辑，端到端保证整体真的能跑。

> 这些文件不匹配 `test*.py`，所以 `python -m unittest discover tests` 不会自动跑到它们
> —— 端到端需要外部服务，不该混进 CI。

## 为什么用「假 Ollama」

1. 没人保证你的机器上有 Ollama，更不保证有模型权重（拉一个 3B 模型要几 GB）；
2. 真模型每次输出都不一样，写不了断言；
3. `fake_ollama.py` 能**精确控制每块的间隔**（默认 50ms），这样才能判断服务端到底有没有
   「攒批」——这是验证 SSE 是不是真流式的关键手段。

## 四步跑起来

```bash
# 0. 凭据走环境变量，不写进代码
export MYSQL_PASSWORD=你的本机 MySQL 密码        # Windows: set MYSQL_PASSWORD=...

# 1. 建临时库 + 用仓库的建表脚本建表 + 灌种子数据
python tests/e2e/setup_db.py

# 2. 终端 A：假 Ollama
python tests/e2e/fake_ollama.py

# 3. 终端 B：起 API（DATABASE_URL / OLLAMA_BASE_URL 指向临时库和假 Ollama）
export DATABASE_URL="mysql+pymysql://root:$MYSQL_PASSWORD@127.0.0.1:3306/chatbot_api_e2e?charset=utf8mb4"
export OLLAMA_BASE_URL=http://127.0.0.1:11435
uvicorn api.main:app --port 8000

# 4. 终端 C：跑冒烟
python tests/e2e/smoke.py
```

`smoke.py` 会逐项打印 PASS/FAIL 并以退出码反映结果（0 = 全过），可以直接接进 CD 流程。

## 前端全链路冒烟：验证「前端 → API → MySQL → Ollama」

```bash
export MYSQL_PASSWORD=你的密码
python tests/e2e/frontend_smoke.py
```

它和 `smoke.py` 的分工是刻意分开的：

- `smoke.py` 用裸 `requests` 打接口 → 验证**服务端**说得对（HTTP 层、落库、SSE 粒度）；
- `frontend_smoke.py` 用**前端真正会用的那个客户端**（`src/api_client.ChatAPIClient`
  + `src/chat_session.APIChatSession`）再走一遍 → 验证**前端拿到的世界对不对**。

它会自动拉起假 Ollama 和 uvicorn（跑完清理），所以只需要 MySQL 在跑。实测输出：

```
── C. 流式对话 ──
[OK]   回复内容与假 Ollama 的输出完全一致 reply='武汉今天多云，22 度，适合出门。'
[OK]   块数与假 Ollama 推的一致 chunks=9
       块到达间隔（毫秒）：[50, 51, 51, 51, 50, 51, 51, 50]
[OK]   最大块间隔 < 200ms（没有攒批） max=51ms
[OK]   拿到服务端实测的生成耗时 468ms
── F. 切换模型 ──
[OK]   服务端确认 model_id 已更新 model_id=2 期望=2
...
结果：30 通过 / 0 失败
```

**为什么要多写这一个脚本。** 接口用 curl 测得完美、前端接上去却是坏的，这种事很常见。
最典型的一个坑是：服务端每 50ms 推一块，前端因为**客户端缓冲**每 200ms 才收到一批。
`smoke.py` 看不到这个问题（它读流的姿势和前端不一样），只有用前端自己的客户端
去读，那个数字才会暴露出来。

一句话：**接口正确 ≠ 界面正确，中间那一层必须单独验。**

## 还有一个：N+1 的实证脚本

```bash
python tests/e2e/count_sql.py
```

它会用 SQLAlchemy 的事件钩子数出「接口实现 vs naive 实现」各发多少条 SQL，并对真实查询
跑 EXPLAIN。实测结果（20 个会话）：

```
A. 接口实现 list_conversations  发 SQL 条数 = 1
B. naive 实现                   发 SQL 条数 = 21
C. 会话列表 EXPLAIN：两张表都是 Using index（覆盖索引，无回表）
   时间范围查询 EXPLAIN：type=ALL + Using filesort（索引存在但不生效）
```

这些数字被引用在 `docs/interview-notes.md` 里 —— 面试时说「我测过，数字是这些」比
「我做了优化」有用得多。

## 字符集：证明「那行声明真的有用」

```bash
python tests/e2e/charset_probe.py
```

`db/models.py` 里给每张表都写了 `mysql_charset=utf8mb4`。但「写了这行代码」和
「这行代码有用」，是两件容易被混为一谈的事：本机练习库本来就是 utf8mb4，
**不加也能跑通**，很容易得出「加不加都一样」的错误结论。

所以这里做一次**受控对照**——故意建一个默认字符集是 `latin1` 的库（敌对环境）：

```
[A] 对照组：裸 DDL 建表，不指定字符集（改动前的行为）
    [OK] 表字符集继承库默认值（即 latin1）        <- collation=latin1_swedish_ci
    [OK] latin1 表写入 emoji 失败                 <- DataError: (1366, "Incorrect string value: ...")
[B] 实验组：仓库 ORM 建表，每张表显式声明 utf8mb4（现在的代码）
    [OK] 建出 6 张表
    [OK] 6 张表 collation 全为 utf8mb4_0900_ai_ci（不受库默认值影响）
[C] 走 ORM 写入并读回 emoji（端到端往返）
    [OK] emoji 经 ORM -> MySQL -> ORM 无损
```

同一个库、同一份数据，只改「建表时有没有声明字符集」，结果一个是写入报错、
一个是无损往返 —— 这才说明那行声明是**有效的**，而不是「数据库恰好是对的」。

> 这个手法可以推广：**凡是「环境恰好正确」才成立的配置，都该造一个敌对环境验一次。**
> 否则你验的是环境的运气，不是代码。

## 容器冒烟：验证「真的在 Docker 里跑起来」

```bash
# 本机有 Docker 时（CI 里也是这个脚本）
bash .github/scripts/container_smoke.sh

# 只想跑断言部分（需要已有一套在跑的服务）
API_BASE=http://127.0.0.1:8000 python tests/e2e/container_smoke.py
```

前面几个脚本验的是「裸进程拼起来对不对」（uvicorn 直起）；这一个验的是
**整套装起来对不对**：镜像能不能构建、容器之间名字解析通不通、容器里的 MySQL
表字符集对不对、凭据有没有被拷进镜像。

编排在 `.github/scripts/container_smoke.sh`，断言在 `container_smoke.py`。
分工的原因是断言只能看 HTTP 层，而「镜像里有没有 `.env`」「容器是不是 root」
「端口有没有映射到宿主机」必须用 `docker CLI` 才看得到。

它的 26 项断言（22 项 HTTP + 4 项容器内）刻意都对着**静态校验够不着**的地方：

| 断言 | 为什么静态校验不够 |
|---|---|
| 镜像里 `ls` 不到 `.env` / `.git` / `tests` | `.dockerignore` 里写了规则 ≠ 规则生效（CRLF 会让它静默失效） |
| 容器内 uid ≠ 0 | `Dockerfile` 写了 `USER appuser` ≠ 生效 |
| mysql / redis 无 `HostPort`、api 有 | 配置写对 ≠ 运行时真的没暴露 |
| `/health` 的 `redis: true` | `REDIS_URL` 用服务名这件事真的生效了 |
| `/health` 的 `ollama: true` | `extra_hosts: host-gateway` 真的解析通了 |
| emoji 经容器内 MySQL 往返无损 | `--character-set-server=utf8mb4` 真的生效了 |
| 每块间隔贴合服务端节奏 | 中间任何一层攒批都会让间隔变成 N 倍 |

两个实测踩到的坑，都写进脚本注释了：

1. **假 Ollama 必须绑 `0.0.0.0`**。容器里的 `host.docker.internal` 解析到的是宿主机在
   docker 网桥上的地址（如 172.17.0.1），不是回环地址。只绑 `127.0.0.1` 时宿主机自己
   curl 得通、容器连不上，报错还是 `Connection refused`，很容易误判成「服务没起」。
2. **别用 `X="$(cmd | tail -1)"` 做检查**。`$()` 的退出码来自管道最后那个 `tail`，
   `cmd` 失败时 `X` 是空的，于是「拿不到结果」被当成「结果正常」，检查静默失效。
   脚本里改成让被测命令输出一个固定标记（如 `LEAKCHECK=CLEAN`），拿不到标记就报错——
   **所有检查都 fail-closed**。

第三条是 2026-09-20 本机首跑时新加的，值得单独说，因为它推翻了前面那句「绑 0.0.0.0 就对了」：

3. **端口通了 ≠ 连到的是我们的假 Ollama。** 开发机上装着一个真的 Ollama
   （占 `127.0.0.1:11434`），而 **Windows 允许 `0.0.0.0:11434` 与它同时绑定**
   （Linux 会直接 `EADDRINUSE`）。于是替身照样起来、脚本打印「已监听」，容器却连到了
   真 Ollama：`/health` 里 `ollama: true`（真 Ollama 答的 `/api/tags`），`/chat` 返回
   404 `model 'llama3.2' not found`，脚本只报「流式返回 0 个 chunk」——**看不出是连错了服务**。
   Linux 上同一个洞换一种表现：替身 bind 失败，而「等端口」被**别人**应答，脚本照样成功。
   所以现在有两处 fail-closed 检查：起替身**之前**确认端口没被占（占用就早停并给出改法：
   停掉它，或 `FAKE_PORT=11500 OLLAMA_BASE_URL=http://host.docker.internal:11500` 换个端口），
   起完再核**指纹**（`/api/tags` 里那条只有替身会返回的记录），并在**容器里**再验一次
   （容器走的路径和宿主机不是同一条）。

> 收尾的规矩：只在「服务是本次运行自己拉起来的」时候才 `docker compose down -v`。
> 在云主机上刚 `deploy.sh` 完再跑它，它检测到服务已在运行，就只做断言、**不会删数据卷**。

## schema 快照：把「与练习库对齐」变成常驻守卫

```bash
# 只有这一步需要真 MySQL：把练习库结构导出成快照（只读 information_schema）
export MYSQL_PASSWORD=你的密码
python tests/e2e/export_schema_snapshot.py

# 守卫本身不需要数据库，跑在 CI 里
python -m unittest tests.test_schema_snapshot
```

`docs/interview-notes.md` 里说过「ORM 与练习库 6 张表是对齐的」。原来这句话只能靠
**手工跑一次**来支撑，跑完就过去了，之后改列 / 改类型 / 改索引都不会有人拦。

现在它变成 CI 里的常驻断言：快照（`tests/data/practice_db_schema.json`）签进仓库，
`tests/test_schema_snapshot.py` 纯文件解析，比对「ORM 定义 vs 快照」。

⚠️ **快照会过期**：库那头改了 schema，守卫会报红。那时要先判断「改动是否有意」，
确认后才重跑导出脚本 —— diff 里能看清到底改了什么。

导出脚本**显式写 LF**（`newline="\n"`）：`Path.write_text` 在 Windows 上会把 `\n`
翻成 `\r\n`，同一份快照在两个平台上导出就会字节不同，diff 里全是噪音。

## 公网入口验收：`public_check.py`（+ 它的靶子 `buffering_proxy.py`）

```bash
# 从**外面**打（你的笔记本），不经过反代的直连也行
python tests/e2e/public_check.py --url https://your-domain.com

# 本地自测用
python tests/e2e/public_check.py --url http://127.0.0.1:8000 --no-ports
```

四个验收脚本的分工是**刻意分开**的，别混：

| 脚本 | 在哪跑 | 需要什么 | 回答什么问题 |
|---|---|---|---|
| `smoke.py` | 本机 | 真 MySQL + 假 Ollama | 这套代码拼起来能跑吗（不经反代） |
| `.github/scripts/container_smoke.sh` | 服务器上 / CI | Docker | **容器化部署**成立吗（镜像无凭据、非 root、端口不外露） |
| `nginx_check.py` | 有 Docker 的机器 | compose 已在跑 | **反代这一层**成立吗（上云之前就能验） |
| `public_check.py` | **从外面** | 一个能访问的 URL | **公网入口**成立吗（真的域名 / IP，经 HTTPS） |

四个里只有 `nginx_check.py` 和 `public_check.py` 能发现「反代把流攒批了」这一类问题 ——
而它恰恰是**最不容易被发现**的：功能完全正常、接口返回正确、落库也对，
只是用户看到的从「一个字一个字蹦」变成「转圈等到最后出全文」。
没有报错、没有告警，只有**到达时刻**能看出来。

所以 `public_check.py` 用裸 socket 记录每块的真实到达时刻，判据在 `src/stream_probe.py`
（那个判据本身也有单测和反向对照，见下）。

`buffering_proxy.py` 是**故意攒批**的替身反代（模拟 `proxy_buffering on`：把上游响应
读完再一次性发，并且像 Nginx 一样不把 `X-Accel-Buffering` 转发给客户端）。
写它的理由：本机当时没有 Docker / Nginx，没有它就没法在上云**之前**复现这个失败模式
（装好 Docker 之后可以再用真 nginx 验一遍，那就是 `nginx_check.py`，见下）：

```bash
python tests/e2e/buffering_proxy.py                                       # 终端 A
python tests/e2e/public_check.py --url http://127.0.0.1:8100 --no-ports    # 终端 B → 流式那条必须 FAIL
```

实测对照（两边同一份代码，只差中间那一层）：

| 路径 | 观测 | 判定 |
|---|---|---|
| 直连 8000 | 9 块分布在 0.404s，首块 0.055s 到达 | ✅ INCREMENTAL |
| 经攒批替身 8100 | 9 块全挤在 0.000s，首块 0.500s 到达（总 0.500s） | ❌ BUFFERED |

⚠️ 端口检查那部分自带**控制组**：先探一个已知开放的端口，连不上就直接报「探针自检失败」，
而不是把后面几个「连不上」当成「安全」—— 否则结论就建立在「什么都没读到」上（假绿）。

## 反代验收：`nginx_check.py`（在**真 nginx** 上量，不是替身）

`public_check.py` 要你先把服务上线、再从另一台机器打进来。这个脚本把同一件事
**提到上云之前**：用**同一个 nginx 镜像、同一份仓库模板**（`deploy/nginx/templates/`）
起一个反代容器，接在正在跑的 compose 上，量 SSE 的到达时刻。

```bash
bash .github/scripts/container_smoke.sh     # 第 9 步就是它（CI 里每次都跑）
python tests/e2e/nginx_check.py            # 也可以单独跑，前提是 compose 已在跑
python tests/e2e/nginx_check.py --keep     # 失败时保留容器，便于进去看
```

它做两件事，**第二件比第一件重要**：

1. 正例：仓库配置 → `api`（真应用）→ 必须判成 **INCREMENTAL**。
2. 反例：把配置里的 `proxy_buffering` 打开 + 上游换成 `plain_sse.py` → 必须判成 **BUFFERED**。

只有正例的话，「通过」什么都证明不了：一把恒真的尺子也长这样。
反例必须红，才能说明这把尺子在这一层量得出东西 —— 和 `tests/test_stream_probe.py`
里的能力检查是同一个道理，只是搬到了运行时。

### 反例为什么用 `plain_sse.py` 而不是真应用

**这是本轮踩出来的一个反直觉结论**：nginx 对 **chunked 分帧**的响应本来就不攒批。
真应用（uvicorn）正是 chunked，所以拿它当反例，开着 `proxy_buffering` 也照样是增量的 ——
反例红不了。`plain_sse.py` 用最朴素的分帧（靠关连接表示结束），那才是经典失败形态。

本机用真 nginx（1.27.4）量出来的完整对照，同一个上游与配置每次只改一个变量：

| 上游分帧 | 防缓冲头 | `proxy_buffering` | 观测（9 块） | 判定 |
|---|---|---|---|---|
| chunked（= 真应用） | 有 | off | 跨度 0.405s，首块 0.019s | ✅ INCREMENTAL |
| chunked（= 真应用） | 有 | on | 跨度 0.404s，首块 0.035s | ✅ INCREMENTAL |
| chunked（= 真应用） | 被 `proxy_ignore_headers` 无视 | on | 跨度 0.404s | ✅ INCREMENTAL |
| 靠关连接结束 | 无 | off | 跨度 0.407s | ✅ INCREMENTAL |
| 靠关连接结束 | 无 | **on** | **跨度 0.000s，首块 0.456s** | ❌ **BUFFERED** |

两点结论：① 对 chunked 分帧，`proxy_buffering` 与那个头在客户端**观测不到差别**；
② 对靠关连接结束的分帧，`proxy_buffering on` 会把整条响应攒完再发。
所以配置里那句 `proxy_buffering off` 是**纵深防御**，不是「救命的那一句」——
配置注释里也是这么写的，没把它说大。

顺带一个**测出来的事实**（之前只是推测）：四种配置下，客户端**都看不到**
`X-Accel-Buffering` 响应头 —— nginx 会消费掉它。所以公网侧的判据只能是到达时刻，
「响应头里有 X-Accel-Buffering」只能作为**应用侧**的断言（`tests/test_chat_stream.py`）。

⚠️ 写反例时踩的坑：第一版想用 `proxy_hide_header X-Accel-Buffering` 去「摘掉」应用那个头。
它只影响**发给客户端**的头，而 nginx 是在上游模块里读到这个头当场就关掉缓冲的 ——
看着把变量摘掉了，其实一点没摘，几个变体全是 INCREMENTAL，像是「配置怎么写都行」。
**受控变量必须真的被控制住**，否则实验结论是假的（真正能无视它的是 `proxy_ignore_headers`）。

⚠️ 第二个坑，**是 CI 上真跑第一次才抓到的**：反例要改的是**两个**变量 ——
`proxy_buffering` **和上游**。第一版只改了前者，直接复用 A 的渲染结果（上游还是 `api:8000`），
于是 B 打到的仍是真应用；而真应用是 chunked 分帧，nginx 对它本来就不攒批 ——
**反例永远红不了**。更麻烦的是它的失败形态不是一条好懂的「判定 = INCREMENTAL」，
而是 `ConnectionResetError`（`POST /` 打到真应用收到 405 后连接被直接切断），
看日志要绕一圈才知道是上游打错了。现在有一条显式断言钉住「反例的上游必须是 stub」。

⚠️ 第三个坑，**修完上面那条之后还是红**：`wait_port()` 把「端口开着」当成了「就绪」。
`docker run -p` 是 **docker-proxy 先把宿主机端口绑上**，容器里的 nginx 随后才（或压根没）
起来 —— 探测以为就绪、接着连上去就被 RST。
**这和 container_smoke 那条「端口 ≠ 就绪」是同一个坑，只是换了层**（那边是 api 最后一个启动，
这边是 docker-proxy 比 nginx 先就绪）。改法有两处：
① 等「真的能收发 HTTP」而不是等端口（任何状态码都算，502 也算 nginx 在服务）；
② 失败时把**容器状态 + 日志**打出来 —— 上一版只剩一句 `ConnectionResetError`，
排查得靠猜。**失败要失败得好懂。**
顺带把 stub 从宿主机挪进同一个 compose 网络：既少一个 `host.docker.internal` 环节，
又和生产里「上游是网络内服务名」同形。

> ⚠️ 值得单独记一笔：这台开发机当时**没有 Docker**，所以 `nginx_check.py` 在上云之前
> **从来没有真正执行过** —— CI 上的第一次运行就是它的第一次运行，而它连着红了两次。
> **「本机验不了」不等于「可以先不验」**：静态守卫能证明「文件里写了正确的规则」，
> 但证明不了「这个脚本自己跑得起来」，后者只有真跑能兜住。
>
> （2026-09-20 补：开发机已经装上 Docker（Desktop 4.91 / 引擎 29.8.0 / compose v5.5.1），
> 于是 `nginx_check.py` 和整套容器冒烟现在**本机就能跑**。但上面那条教训不变 ——
> 它能被本机跑，是因为有人先把「这东西从没跑过」当成缺陷去处理了。）

## 反向对照：证明「守卫真的会红」

```bash
python tests/e2e/reverse_check.py                  # 六个分组都跑
python tests/e2e/reverse_check.py schema           # 只跑一组
python tests/e2e/reverse_check.py container_smoke  # 只跑一组
python tests/e2e/reverse_check.py stream_probe     # 只跑一组
python tests/e2e/reverse_check.py nginx            # 只跑一组
python tests/e2e/reverse_check.py deploy_script    # 只跑一组
```

**「测试全绿」不能证明测试有效** —— 断言写松了、写成恒真条件，一样全绿。
唯一可靠的判据是把缺陷**种回去**，看它会不会失败。这个脚本自动做这件事：

| 分组 | 种的缺陷 | 结果 |
|---|---|---|
| `chat_stream` | Content-Type / 防缓冲头 / SSE 空行分帧 / done 字段 / 404 校验 / 断连报错 / latency_ms 落库 | 7/7 全红 |
| `schema` | 缺列 / 类型 / 枚举取值 / 可空性 / 索引 / 字符集声明 / 符号台账 / 快照少一张表 | 8/8 全红 |
| `container_smoke` | 端口断言不等就绪 / 丢空状态健全性检查 / 端口缺失不再失败 / 假 Ollama 的端口守卫恒判空闲 | 4/4 全红 |
| `stream_probe` | 尺子不判跨度 / 块数不足当通过 / 尺子不判首块位置 / 阈值参数不接线 | 4/4 全红 |
| `nginx` | 不关缓冲 / 退化成 HTTP1.0 / Connection 没置空 / 超时退回 60s / 上游写死 127.0.0.1 / 占位符小写 / 模板没挂进容器 / 模板不钉 LF | 8/8 全红 |
| `deploy_script` | 密码行缺失时静默退出 / 空密码 / 默认密码 / 含 `@` 密码 / 缺 .env 不给修法 / 参数打错不报错 / `--no-pull` 失效 / `--proxy` 不查 API_BIND / 起反代不带 profile / 就绪承诺与实现脱钩 / 不就绪也报成功 / 部署脚本删数据卷 | 12/12 全红 |

**后三组守的不是业务代码，而是判据本身** —— 一个量不出东西的尺子，
和一个量出「一切正常」的尺子长得一模一样。`stream_probe` 那把尤其值得守：
它错的时候部署是**能用的**，只是从「一个个蹦字」变成「转圈等到最后出全文」。
`nginx` 那一组的性质特殊：它守的三类文件（模板 / compose / `.gitattributes`）
**错了本机毫无症状**（本机没装 nginx），要等 CI 第 9 步在真容器上量到达时刻才暴露。

> 这一组第一次跑就抓到了自己的问题：**「尺子不判跨度」那条变异没变红**——
> 现实样本里「一起到达」几乎总伴随「首块来得太晚」，于是另一条判据先命中了，
> 把跨度判据遮住了。也就是说当时**没有任何一条用例能单独钉住跨度判据**。
> 补了一个隔离样本（3 块挤在 4ms 内、但首块相对总耗时很早）才把它钉住。
> 这就是为什么「守卫要能被证明会红」——读代码是看不出来的。

最后一组值得单独说一句：它守的不是 Python 代码，而是 `container_smoke.sh` 里
**api 端口那条断言的等待逻辑**。那条断言原本是「立刻 inspect，没有 HostPort 就失败」，
而 api 在 compose 里是三个容器中最后一个启动的（`depends_on: service_healthy`），
脚本常在它 Up 后不到 1 秒就走到断言处 —— 那一刻 Docker 还没把端口绑定写进
`NetworkSettings.Ports`，inspect 回来是 `{}`，于是 CI 假红一次（**同一次提交原样重跑就绿**）。
修法是把断言改成「等 30s，等不到仍报错」。但「改完 CI 绿了」证明不了任何事 ——
这条断言本来就 flaky，绿是它的常态。所以 `tests/test_container_smoke_script.py`
抽出脚本原文 + stub 掉 `docker` 命令，用三种确定性场景证明等待逻辑成立：
晚到会等、始终缺失会失败、`inspect` 拿到空状态时不能把「什么都没读到」当成「安全」。

新的一组 `deploy_script`（第六组）守的是**上机第一步那个脚本**，性质和上面这条一样：
`deploy.sh` 长期处于「谁也没执行过」的状态 —— CI 直接跑 `container_smoke.sh`，
静态校验（`tests/test_deploy_manifest.py`）只把它当**文本**读（查行尾、查字符串）。
第一次真跑（替身 `docker`）就抓到一个静态校验永远看不见的洞：`.env` 里少一行
`MYSQL_ROOT_PASSWORD=` 时，脚本**连一句输出都没有**就退出（`set -e` + `pipefail`）。
现在它被两处钉住：`tests/test_deploy_script.py`（替身 `docker`，秒级走完每条分支）
与 `container_smoke.sh` 第 5 步（在真 Docker 上真跑同一条命令）。
于是服务器上那条 `bash deploy.sh && bash .github/scripts/container_smoke.sh`
和 CI 里跑的**已经是同一件事**。

> 这一组的第一版还犯了个值得记下来的错：**锚点打错了位置**。
> 原以为把 `read_env` 里那句 `|| true` 拆掉就能重现「静默退出」，种回去之后测试却没红 ——
> 那条用例等于没被验证过。量下来才发现：**`set -e` 不会中止命令替换的子壳**
> （`echo "$(false; echo INSIDE)"` 会打印 INSIDE 并继续；Linux bash 5.2.37 与
> Cygwin bash 5.3.15 结论一致），子壳的退出码只看它最后一条命令；而那个函数以 `printf`
> 收尾，于是失败被**转成了空串**，由调用方那句 `[[ -n "$PW" ]] || die` 兜住。
> 所以锚点必须打在**调用点**。顺带得到一个结论：
> **「静默退出」和「变成空串」是两种坏法，兜住它们的是不同的东西。**

三条设计上的硬要求，都写在脚本头部注释里：

1. **fail-closed**：任何一条「种回缺陷后还是绿的」都算失败；锚点找不到就**直接中止**，
   绝不静默跳过 —— 否则脚本会假装跑过。
2. **按字节还原 + 校验 sha256**。文本模式的 read/write 在 Windows 上会来回翻译
   `\n` 与 `\r\n`，往返一趟就可能改写文件。**真踩过：把 `api/routers/chat.py` 写成
   172 行 `\r\r\n`。** 那次恰好是脚本自检报了「文件没还原干净」——
   **自检报的警必须查，不能归类成「格式噪音」。**
3. 改一个、跑一个、立刻还原，不留中间态。

> ⚠️ 这个脚本**会修改仓库里的源码文件**（改完立刻还原）。所以它放 `tests/e2e/`
> 而不是 `tests/` —— 不匹配 `test*.py`，不进 CI、不会被误跑。
> 运行期间不要同时编辑 `api/routers/chat.py` / `db/models.py` / 快照文件。

## 一个小提醒

`setup_db.py` 会 **DROP 再 CREATE** 临时库（默认 `chatbot_api_e2e`）。别把它指向你的
练习库：`E2E_DB` 换名字即可，但千万别设成 `chatbot`。

`charset_probe.py` 同理，它会重建 `chatbot_charset_probe` 这个库（跑完自动 drop），
库名可用 `E2E_CHARSET_DB` 改。
