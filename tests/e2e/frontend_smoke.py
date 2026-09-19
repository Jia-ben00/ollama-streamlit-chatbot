"""前端客户端端到端冒烟：验证「Streamlit 前端 → API → MySQL → Ollama」整条链路。

和 `smoke.py` 的分工：
- `smoke.py` 用裸 `requests` 打接口，验证**服务端**说得对（HTTP 层、落库、SSE 粒度）。
- 本脚本用**前端真正会用的那个客户端**（`src/api_client.ChatAPIClient` +
  `src/chat_session.APIChatSession`）再跑一遍，验证**前端拿到的世界对不对**。

为什么非要分开：这两件事经常不一致。接口用 curl 测得完美，前端接上去却是坏的——
最典型的就是「SSE 服务端每 50ms 推一块，前端每 200ms 收到一批」（客户端缓冲），
以及「接口返回 model_id，界面要显示模型名」（前端少做一层转换）。
**接口正确不等于界面正确**，中间那一层必须单独验。

本脚本会自动拉起假 Ollama 和 uvicorn（跑完清理），所以只需要 MySQL：
    set MYSQL_PASSWORD=你的密码
    python tests/e2e/frontend_smoke.py
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
E2E_DIR = Path(__file__).resolve().parent

# Windows 控制台默认 GBK，打印非 ASCII 前必须切编码（否则直接崩）。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
E2E_DB = os.getenv("E2E_DB", "chatbot_api_e2e")

API_PORT = int(os.getenv("E2E_API_PORT", "8000"))
OLLAMA_PORT = int(os.getenv("FAKE_OLLAMA_PORT", "11435"))

API_BASE = f"http://127.0.0.1:{API_PORT}"
OLLAMA_BASE = f"http://127.0.0.1:{OLLAMA_PORT}"

EXPECTED_REPLY = "武汉今天多云，22 度，适合出门。"  # fake_ollama.CHUNKS 拼起来的结果
EXPECTED_CHUNKS = 9
CHUNK_DELAY_MS = 50  # 与 fake_ollama 的 FAKE_OLLAMA_DELAY 一致

ok_count = 0
failures = []


def check(name: str, cond: bool, extra: str = "") -> bool:
    global ok_count
    if cond:
        ok_count += 1
        print(f"[OK]   {name} {extra}")
    else:
        failures.append(name)
        print(f"[FAIL] {name} {extra}")
    return cond


def wait_port(host: str, port: int, timeout: float = 25) -> bool:
    """等一个端口能连上（比固定 sleep 可靠：机器快就快，慢就多等）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.3)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def seed_second_model():
    """往临时库补第二个模型，用来验证「前端切换模型」这条路径。

    过不了 API 插（没有 POST /models 接口），所以直连数据库。
    e2e 场景里这么做是可以的：它就是在准备**前置状态**，
    而不是在绕过被测逻辑。
    """
    import pymysql

    conn = pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
        password=MYSQL_PASSWORD, database=E2E_DB, charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO models (name, provider, param_size, context_window, is_active) "
                "VALUES (%s, %s, %s, %s, 1)",
                ("qwen2:0.5b", "ollama", "0.5B", 32768),
            )
        conn.commit()
    finally:
        conn.close()


def prepare_database():
    """建临时库 + 建表 + 灌种子（复用 smoke.py 用的那套脚本）。"""
    env = os.environ.copy()
    env["MYSQL_PASSWORD"] = MYSQL_PASSWORD
    env["E2E_DB"] = E2E_DB
    env["MYSQL_HOST"] = MYSQL_HOST
    env["MYSQL_PORT"] = str(MYSQL_PORT)
    env["MYSQL_USER"] = MYSQL_USER
    result = subprocess.run(
        [sys.executable, str(E2E_DIR / "setup_db.py")],
        cwd=str(REPO), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise SystemExit("setup_db.py 失败，见上面的输出")
    seed_second_model()
    print("[环境] 临时库就绪，并补了第二个模型")


def run_checks() -> None:
    from src.api_client import APIUnreachable, ChatAPIClient, ChatAPIError
    from src.chat_session import APIChatSession

    client = ChatAPIClient(base_url=API_BASE)

    # ── A. 前端看到的健康状态 ────────────────────────────
    print("\n── A. 健康与目录 ──")
    health = client.health()
    checks = health["checks"]
    check("前端读到 /health", "status" in health, f"status={health['status']}")
    check("数据库探针为真", checks["database"] is True)
    check("Ollama 探针为真（指向假 Ollama）", checks["ollama"] is True)
    # Redis 在本机没起，所以这一项**不强制通过**——e2e 不该依赖
    # 「碰巧装了 Redis」。降级能力本身有单元测试守着。
    print(f"       （redis={checks['redis']}，本机无 Redis 属预期，缓存降级不影响功能）")

    models = client.list_models()
    check("模型目录至少 2 个", len(models) >= 2, f"names={[m['name'] for m in models]}")

    users = client.list_users()
    check("用户目录非空", len(users) >= 1, f"users={[u['username'] for u in users]}")
    check("用户信息不含 email（PII 白名单）", users and "email" not in users[0])

    # ── B. 会话生命周期（前端视角）───────────────────────
    print("\n── B. 会话生命周期 ──")
    session = APIChatSession(client, user_id=None)
    model_names = session.refresh_models()
    check("refresh_models 建立 name→id 映射", len(session._model_ids) >= 2)

    first_model = model_names[0]
    conv_id = session.create_conversation("前端联调会话", first_model)
    check("新建会话返回 id", isinstance(conv_id, int) and conv_id > 0, f"id={conv_id}")
    check("新建后自动成为当前会话", session.conversation_id == conv_id)

    conversations = session.list_conversations()
    check(
        "会话列表包含新建的会话",
        any(c["id"] == conv_id for c in conversations),
        f"共 {len(conversations)} 个会话",
    )

    # ── C. 流式对话：前端读到的粒度（本脚本的核心）────────
    print("\n── C. 流式对话 ──")
    chunks = []
    gaps = []
    prev = None
    for piece in session.send("今天天气怎么样"):
        now = time.perf_counter()
        if prev is not None:
            gaps.append((now - prev) * 1000)
        prev = now
        chunks.append(piece)

    reply = "".join(chunks)
    check("回复内容与假 Ollama 的输出完全一致", reply == EXPECTED_REPLY, f"reply={reply!r}")
    check("块数与假 Ollama 推的一致", len(chunks) == EXPECTED_CHUNKS, f"chunks={len(chunks)}")

    if gaps:
        body = sorted(gaps)
        median = body[len(body) // 2]
        print(f"       块到达间隔（毫秒）：{[round(g) for g in gaps]}")
        # 假 Ollama 每 50ms 推一块。如果前端这一侧存在攒批（比如 iter_lines 用了
        # 默认 chunk_size=512），间隔会翻倍成 100/150/200ms，这条断言就会红。
        # 阈值放到 200ms 是留了余量（CI 机器抖动），但已经足够拦住「攒批」。
        check("最大块间隔 < 200ms（没有攒批）", max(gaps) < 200, f"max={max(gaps):.0f}ms")
        check("块间隔中位数接近 50ms", median < 120, f"median={median:.0f}ms")

    latency = session.last_latency_ms
    check("拿到服务端实测的生成耗时", isinstance(latency, int) and latency > 0, f"{latency}ms")

    # ── D. 落库与读回 ────────────────────────────────────
    print("\n── D. 落库与读回 ──")
    messages = session.history()
    check("历史共 2 条（user + assistant）", len(messages) == 2, f"count={len(messages)}")
    if len(messages) == 2:
        check("顺序为 user → assistant", [m["role"] for m in messages] == ["user", "assistant"])
        check("用户消息内容正确", messages[0]["content"] == "今天天气怎么样")
        check("助手消息内容完整", messages[1]["content"] == EXPECTED_REPLY)

    raw = client.list_messages(conv_id)
    assistant_row = next((m for m in raw if m["role"] == "assistant"), None)
    check(
        "assistant 消息落库时带了 latency_ms",
        assistant_row is not None and assistant_row["latency_ms"] is not None,
        f"latency_ms={assistant_row and assistant_row['latency_ms']}",
    )

    # ── E. 多轮上下文续接 ────────────────────────────────
    print("\n── E. 多轮对话 ──")
    second = "".join(session.send("那明天呢"))
    check("第二轮也能正常回复", second == EXPECTED_REPLY)
    check("历史累积到 4 条", len(session.history()) == 4, f"count={len(session.history())}")

    # ── F. 切换模型 ──────────────────────────────────────
    print("\n── F. 切换模型 ──")
    if len(model_names) >= 2:
        second_model = model_names[1]
        session.set_model(second_model)
        check(
            "切换后会话绑定的是新模型",
            session.current_model() == second_model,
            f"current={session.current_model()!r}",
        )
        # 从服务端独立确认一次：前端显示的模型名，对应的 id 真的是写进库的那个。
        # 「界面显示了新模型」和「库里真的换了」是两件事，都要验。
        conv = client.get_conversation(conv_id)
        second_id = next(
            m["id"] for m in client.list_models() if m["name"] == second_model
        )
        check(
            "服务端确认 model_id 已更新",
            conv["model_id"] == second_id,
            f"model_id={conv['model_id']} 期望={second_id}",
        )
    else:
        check("有第二个模型可用于切换测试", False, "模型数不足")

    # ── G. 清空对话 ──────────────────────────────────────
    print("\n── G. 清空对话 ──")
    deleted = client.clear_messages(conv_id)
    check("清空返回删除条数", deleted == 4, f"deleted={deleted}")
    check("清空后前端历史为空", session.history() == [])
    check(
        "会话本身仍在（清空的语义是删消息，不是删会话）",
        client.get_conversation(conv_id)["id"] == conv_id,
    )
    # 界面上的「清空」按钮走的是会话层，也验一下它能工作在空会话上。
    session.clear()
    check("会话层清空可重复调用且不报错", session.history() == [])

    # ── H. 错误路径 ──────────────────────────────────────
    print("\n── H. 错误处理 ──")
    try:
        list(client.chat_stream(999999, "hi"))
        check("对不存在的会话发消息会报错", False, "没有抛异常")
    except ChatAPIError as exc:
        check("对不存在的会话发消息 → ChatAPIError", True, f"detail={exc}")
    except Exception as exc:  # noqa: BLE001
        check("对不存在的会话发消息 → ChatAPIError", False, f"抛的是 {type(exc).__name__}")

    dead = ChatAPIClient(base_url="http://127.0.0.1:9", timeout=(2, 2))
    try:
        dead.health()
        check("连不上服务时抛 APIUnreachable", False, "没有抛异常")
    except APIUnreachable as exc:
        check("连不上服务时抛 APIUnreachable", True, f"{str(exc)[:40]}...")
    except Exception as exc:  # noqa: BLE001
        check("连不上服务时抛 APIUnreachable", False, f"抛的是 {type(exc).__name__}")


def main() -> int:
    if not MYSQL_PASSWORD:
        print("[FAIL] 未设置 MYSQL_PASSWORD。凭据不写进代码，请通过环境变量提供。")
        return 2

    prepare_database()

    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{E2E_DB}"
        f"?charset=utf8mb4"
    )
    env["OLLAMA_BASE_URL"] = OLLAMA_BASE
    env["OLLAMA_TIMEOUT"] = "30"
    env["FAKE_OLLAMA_PORT"] = str(OLLAMA_PORT)

    procs = []
    try:
        print(f"[环境] 启动假 Ollama @ {OLLAMA_BASE}")
        procs.append(
            subprocess.Popen(
                [sys.executable, str(E2E_DIR / "fake_ollama.py")],
                cwd=str(REPO), env=env,
            )
        )
        if not wait_port("127.0.0.1", OLLAMA_PORT):
            raise SystemExit("假 Ollama 没起来")

        print(f"[环境] 启动 API @ {API_BASE}")
        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "api.main:app",
                 "--port", str(API_PORT), "--log-level", "warning"],
                cwd=str(REPO), env=env,
            )
        )
        if not wait_port("127.0.0.1", API_PORT):
            raise SystemExit("API 没起来")

        print("[环境] 就绪，开始断言\n" + "=" * 60)
        run_checks()
    finally:
        for proc in reversed(procs):
            proc.terminate()
        for proc in reversed(procs):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    print("=" * 60)
    print(f"结果：{ok_count} 通过 / {len(failures)} 失败")
    if failures:
        for name in failures:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
