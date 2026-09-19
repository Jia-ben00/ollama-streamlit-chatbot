"""端到端冒烟测试：对着「真跑起来的 API」打全链路，逐项断言。

和 tests/test_api.py 的区别：
- `test_api.py` 是**单元测试**：mock 掉 DB/Ollama/Redis，验证路由与参数校验，秒级完成；
- 这个脚本是**端到端**：真 MySQL + 真 HTTP + 真 SSE + 假 Ollama，验证「拼起来能跑」。

两者都要有。单元测试保证改代码不破坏局部逻辑，端到端保证「各层拼起来是通的」——
CI 里跑前者（快、无外部依赖），上线前后跑后者。

前置（三个终端，见 tests/e2e/setup_db.py 输出）：
    A: python tests/e2e/fake_ollama.py
    B: uvicorn api.main:app --port 8000        （DATABASE_URL / OLLAMA_BASE_URL 已设好）
    C: python tests/e2e/smoke.py

环境变量：
    API_BASE       默认 http://127.0.0.1:8000
    MYSQL_* / E2E_DB  用于直连数据库复核落库结果（不设则跳过该部分）
"""

import json
import os
import socket
import sys
import time
from urllib.parse import urlparse

import requests

# Windows 控制台默认是 GBK，直接打印 emoji 会抛
#   UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f600'
# 而本脚本恰好要打印含 emoji 的消息内容。所以先把标准输出切成 UTF-8。
# （不改这一行，脚本会在"验证 emoji"这一步自己崩掉——挺讽刺，但正是这种地方最容易漏。）
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 重定向到文件时不要输出 ANSI 转义码，避免日志里全是 [32m。
USE_COLOR = sys.stdout.isatty()

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000")
PARSED = urlparse(API_BASE)
HOST, PORT = PARSED.hostname or "127.0.0.1", PARSED.port or 80
CHUNK_COUNT = 9  # 与 fake_ollama.CHUNKS 的长度一致

failures = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if USE_COLOR:
        mark = "\033[32mPASS\033[0m" if cond else "\033[31mFAIL\033[0m"
    else:
        mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


def sse_raw(conversation_id: int, content: str, timeout: float = 15.0):
    """裸 socket 打 POST /chat，拿每块的真实到达时刻。

    为什么不用 requests.iter_lines：它按 chunk_size 阻塞读（默认 512 字节），
    数据不满就等着，会把「服务端每 50ms 推一块」看成「每 200ms 来一批」。
    要判断服务端有没有攒批，必须绕开这层缓冲。这个坑本身见 docs/interview-notes.md。
    """
    body = json.dumps({"conversation_id": conversation_id, "content": content}).encode("utf-8")
    head = (
        f"POST /chat HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")

    sock = socket.create_connection((HOST, PORT), timeout=timeout)
    sock.sendall(head + body)

    buf, headers, events, header_done = b"", "", [], False
    t0 = time.monotonic()
    while True:
        data = sock.recv(4096)
        if not data:
            break
        now = time.monotonic() - t0
        buf += data
        if not header_done and b"\r\n\r\n" in buf:
            raw_head, buf = buf.split(b"\r\n\r\n", 1)
            headers = raw_head.decode("utf-8", "replace")
            header_done = True
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data: "):
                events.append((json.loads(line[6:].decode("utf-8")), round(now, 3)))
        if time.monotonic() - t0 > timeout:
            break
        if events and events[-1][0].get("done"):
            break
    total = round(time.monotonic() - t0, 3)
    sock.close()
    return headers, events, total


def main() -> int:
    s = requests.Session()

    # ── 0. 服务是否在跑 ──────────────────────────────────
    try:
        r = s.get(f"{API_BASE}/", timeout=5)
    except requests.RequestException as exc:
        print(f"[FAIL] 连不上 {API_BASE}：{exc}")
        print("       先起服务：uvicorn api.main:app --port 8000")
        return 2

    check("GET / 返回服务信息",
          r.status_code == 200 and r.json().get("service") == "ollama-chatbot-api")

    # ── 1. 健康检查：存活 vs 就绪 ────────────────────────
    h = s.get(f"{API_BASE}/health", timeout=10).json()
    check("GET /health 数据库探针可用", h["checks"]["database"] is True, f"body={h}")
    check("GET /health Ollama 探针可用（假 Ollama 在跑）", h["checks"]["ollama"] is True)
    check("GET /health 在依赖缺失时仍是 200（存活探针的语义）",
          s.get(f"{API_BASE}/health", timeout=10).status_code == 200)
    ready_code = s.get(f"{API_BASE}/health/ready", timeout=10).status_code
    check("GET /health/ready 与依赖状态一致", ready_code in (200, 503),
          f"HTTP {ready_code}（Redis 没起时 503 是预期行为）")

    # ── 2. 会话列表（N+1 防护的那条 SQL）──────────────────
    convs = s.get(f"{API_BASE}/conversations", params={"user_id": 1}, timeout=10).json()
    seeded = [c for c in convs if c["title"].startswith("会话 ")]
    check("GET /conversations 返回种子会话", len(seeded) == 20, f"count={len(seeded)}")
    check("会话列表带 message_count（单条 SQL 聚合）",
          all(c["message_count"] == 5 for c in seeded), "每个会话 5 条")
    cid = seeded[0]["id"]

    # ── 3. 消息列表 + 游标分页 ────────────────────────────
    msgs = s.get(f"{API_BASE}/conversations/{cid}/messages", timeout=10).json()
    check("GET /messages 返回 5 条且按时间正序",
          len(msgs) == 5 and msgs[0]["id"] < msgs[-1]["id"], f"ids={[m['id'] for m in msgs]}")

    page = s.get(f"{API_BASE}/conversations/{cid}/messages",
                 params={"before_id": msgs[2]["id"], "limit": 2}, timeout=10).json()
    check("游标分页 before_id 生效",
          len(page) == 2 and all(m["id"] < msgs[2]["id"] for m in page),
          f"ids={[m['id'] for m in page]}")

    check("不存在的会话返回 404（而非空数组）",
          s.get(f"{API_BASE}/conversations/999999/messages", timeout=10).status_code == 404)

    # ── 4. 创建会话 ──────────────────────────────────────
    r = s.post(f"{API_BASE}/conversations",
               json={"title": "端到端联调会话", "model_id": 1, "user_id": 1}, timeout=10)
    check("POST /conversations 创建成功", r.status_code == 201, f"id={r.json().get('id')}")
    new_id = r.json()["id"]
    emoji_id = s.post(f"{API_BASE}/conversations",
                      json={"title": "emoji 会话", "model_id": 1, "user_id": 1},
                      timeout=10).json()["id"]

    # ── 5. 流式聊天 ──────────────────────────────────────
    prompt = "武汉今天天气怎么样？😀🔥"
    headers, events, total = sse_raw(new_id, prompt)

    check("POST /chat 的 Content-Type 是 text/event-stream",
          "text/event-stream" in headers.lower(),
          next((l for l in headers.split("\r\n") if l.lower().startswith("content-type")), "?"))

    chunks = [e[0]["chunk"] for e in events if "chunk" in e[0]]
    done = [e for e in events if e[0].get("done")]
    arrivals = [e[1] for e in events if "chunk" in e[0]]

    check(f"流式返回 {CHUNK_COUNT} 个 chunk", len(chunks) == CHUNK_COUNT,
          f"reply={''.join(chunks)}")
    check("流以 done 事件收尾且带 latency_ms",
          len(done) == 1 and done[0][0]["latency_ms"] >= 0,
          f"latency_ms={done[0][0]['latency_ms'] if done else 'N/A'}")

    gaps = [round(arrivals[i + 1] - arrivals[i], 3) for i in range(len(arrivals) - 1)]
    check("每块间隔贴合服务端设定的 50ms（服务端没有攒批）",
          bool(gaps) and all(0.02 <= g <= 0.15 for g in gaps), f"gaps={gaps}")
    check("首块很快到达（没等生成完才返回）", arrivals[0] < 0.15,
          f"首块 {arrivals[0]}s / 总 {total}s")

    # ── 6. 落库复核 ──────────────────────────────────────
    detail = s.get(f"{API_BASE}/conversations/{new_id}", timeout=10).json()
    check("聊天后 message_count = 2（user + assistant）",
          detail["message_count"] == 2, f"message_count={detail['message_count']}")

    stored = s.get(f"{API_BASE}/conversations/{new_id}/messages", timeout=10).json()
    check("消息角色顺序为 user -> assistant",
          [m["role"] for m in stored] == ["user", "assistant"])
    check("user 消息内容不被改写", stored[0]["content"] == prompt, stored[0]["content"])
    check("assistant 消息写了 latency_ms", stored[1]["latency_ms"] is not None,
          f"latency_ms={stored[1]['latency_ms']}")

    # ── 7. emoji 往返（utf8mb4 实证）──────────────────────
    sse_raw(emoji_id, "表情测试 🚀😀🔥")
    rows = s.get(f"{API_BASE}/conversations/{emoji_id}/messages", timeout=10).json()
    check("emoji 经 HTTP -> MySQL -> HTTP 往返无损",
          rows[0]["content"] == "表情测试 🚀😀🔥", rows[0]["content"])

    # ── 8. 错误分支 ──────────────────────────────────────
    r = s.post(f"{API_BASE}/chat", json={"conversation_id": 999999, "content": "hi"}, timeout=10)
    check("给不存在的会话发消息返回 404", r.status_code == 404)
    r = s.post(f"{API_BASE}/chat", json={"conversation_id": new_id, "content": ""}, timeout=10)
    check("空消息被 Pydantic 拦下返回 422", r.status_code == 422)

    # ── 9. 归档 ──────────────────────────────────────────
    r = s.patch(f"{API_BASE}/conversations/{new_id}", timeout=10)
    check("PATCH 归档把 is_archived 置为 true", r.json()["is_archived"] is True)

    # ── 10. 直连 MySQL 复核（可选，需要凭据）──────────────
    pw = os.getenv("MYSQL_PASSWORD", "")
    if pw:
        import pymysql

        dbname = os.getenv("E2E_DB", "chatbot_api_e2e")
        conn = pymysql.connect(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "root"),
            password=pw,
            database=dbname,
            charset="utf8mb4",
        )
        with conn.cursor() as cur:
            cur.execute("SELECT role, content, latency_ms FROM messages WHERE conversation_id=%s ORDER BY id",
                        (new_id,))
            db_rows = cur.fetchall()
            cur.execute("SELECT content FROM messages WHERE conversation_id=%s ORDER BY id LIMIT 1",
                        (emoji_id,))
            emoji_db = cur.fetchone()[0]
            cur.execute("SELECT @@character_set_connection")
            charset_conn = cur.fetchone()[0]
        conn.close()

        check("直连 MySQL 复核：2 条消息真的落库", len(db_rows) == 2)
        check("直连 MySQL 复核：assistant 的 latency_ms 非空", db_rows[1][2] is not None,
              f"latency_ms={db_rows[1][2]}")
        check("直连 MySQL 复核：emoji 在库里就是 4 字节 utf8mb4", emoji_db == "表情测试 🚀😀🔥")
        check("连接字符集是 utf8mb4", charset_conn == "utf8mb4", charset_conn)
    else:
        print("[SKIP] 未设置 MYSQL_PASSWORD，跳过直连数据库复核（HTTP 层结果依然有效）")

    print()
    if failures:
        print(f"{len(failures)} 项失败：" + "; ".join(failures))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
