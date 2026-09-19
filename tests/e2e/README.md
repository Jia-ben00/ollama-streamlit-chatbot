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

## 反向对照：证明「守卫真的会红」

```bash
python tests/e2e/reverse_check.py            # 两个分组都跑
python tests/e2e/reverse_check.py schema     # 只跑一组
```

**「测试全绿」不能证明测试有效** —— 断言写松了、写成恒真条件，一样全绿。
唯一可靠的判据是把缺陷**种回去**，看它会不会失败。这个脚本自动做这件事：

| 分组 | 种的缺陷 | 结果 |
|---|---|---|
| `chat_stream` | Content-Type / 防缓冲头 / SSE 空行分帧 / done 字段 / 404 校验 / 断连报错 / latency_ms 落库 | 7/7 全红 |
| `schema` | 缺列 / 类型 / 枚举取值 / 可空性 / 索引 / 字符集声明 / 符号台账 / 快照少一张表 | 8/8 全红 |

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
