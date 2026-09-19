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
