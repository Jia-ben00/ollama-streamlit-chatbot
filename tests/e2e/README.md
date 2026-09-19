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

## 一个小提醒

`setup_db.py` 会 **DROP 再 CREATE** 临时库（默认 `chatbot_api_e2e`）。别把它指向你的
练习库：`E2E_DB` 换名字即可，但千万别设成 `chatbot`。
