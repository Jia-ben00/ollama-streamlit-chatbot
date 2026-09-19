"""字符集「剂量-反应」验证：证明表字符集是**代码声明**在起作用，不是服务器默认值恰好正确。

为什么值得单独测一次：
`db/models.py` 里给每张表加了 `mysql_charset=utf8mb4` / `mysql_collate=utf8mb4_0900_ai_ci`。
但「加了这行代码」和「这行代码真的有用」是两件事 —— 本机练习库本来就是 utf8mb4，
不加也能跑通，很容易得出「加不加都一样」的错误结论。

所以这里做一次**受控对照**：故意建一个默认字符集为 latin1 的库（敌对环境），
   A. 用不带字符集的裸 DDL 建表（= 改动前的行为）→ 预期仍是 latin1，emoji 写不进去；
   B. 用仓库的 ORM 建表（= 现在的代码）→ 预期是 utf8mb4，emoji 能往返。
两次结果不同，才说明那行声明是**有效的**，而不是碰巧。

顺带解释了面试第 6 问：MySQL 的 `utf8` 只有 3 字节，emoji 是 4 字节，必须 `utf8mb4`。

凭据一律从环境变量读：
    MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD

用法：
    set MYSQL_PASSWORD=你的密码 && python tests/e2e/charset_probe.py

退出码 0 = 全部符合预期。这个脚本需要真实 MySQL，放 e2e 目录、不匹配 test*.py，不进 CI。
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    # Windows 控制台是 GBK，直接打印非 BMP 字符会抛 UnicodeEncodeError。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
PORT = int(os.getenv("MYSQL_PORT", "3306"))
USER = os.getenv("MYSQL_USER", "root")
PASSWORD = os.getenv("MYSQL_PASSWORD", "")
PROBE_DB = os.getenv("E2E_CHARSET_DB", "chatbot_charset_probe")

# 4 字节字符：emoji。utf8(3 字节) 存不下，utf8mb4 才能存。
EMOJI = "表情测试 🚀😀🔥"

_failures = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "[OK]  " if ok else "[FAIL]"
    print(f"  {mark} {label}" + (f"  <- {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def raw_conn(database: str | None = None):
    import pymysql

    return pymysql.connect(
        host=HOST, port=PORT, user=USER, password=PASSWORD,
        database=database, charset="utf8mb4", autocommit=True,
    )


def table_collation(conn, db_name: str, table: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_collation FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (db_name, table),
        )
        row = cur.fetchone()
        return (row[0] if row and row[0] else "") or ""


def main() -> int:
    if not PASSWORD:
        print("[FAIL] 没有设置 MYSQL_PASSWORD 环境变量。凭据不写进代码，请通过环境变量提供。")
        return 2

    admin = raw_conn()
    try:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{PROBE_DB}`")
            # 关键：故意把库默认字符集设成 latin1（敌对环境）。
            cur.execute(
                f"CREATE DATABASE `{PROBE_DB}` CHARACTER SET latin1 COLLATE latin1_swedish_ci"
            )
    finally:
        admin.close()
    print(f"[0] 已建敌对库 {PROBE_DB}（默认字符集 latin1 —— 故意设置）")

    # ── A. 对照组：不带字符集的裸 DDL（模拟「改动前」）──────────
    print("\n[A] 对照组：裸 DDL 建表，不指定字符集（改动前的行为）")
    conn = raw_conn(PROBE_DB)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE cmp_raw (id INT PRIMARY KEY AUTO_INCREMENT, content TEXT)")
        coll = table_collation(conn, PROBE_DB, "cmp_raw")
        check("表字符集继承库默认值（即 latin1）", coll.startswith("latin1"), f"collation={coll}")

        emoji_failed = False
        detail = ""
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO cmp_raw (content) VALUES (%s)", (EMOJI,))
        except Exception as exc:  # pymysql.err.DataError: Incorrect string value
            emoji_failed = True
            detail = type(exc).__name__ + ": " + str(exc).strip().splitlines()[0][:90]
        check("latin1 表写入 emoji 失败", emoji_failed, detail)
    finally:
        conn.close()

    # ── B. 实验组：仓库的 ORM 建表（现在的代码）─────────────────
    print("\n[B] 实验组：仓库 ORM 建表，每张表显式声明 utf8mb4（现在的代码）")
    os.environ["DATABASE_URL"] = (
        f"mysql+pymysql://{USER}:{PASSWORD}@{HOST}:{PORT}/{PROBE_DB}?charset=utf8mb4"
    )
    from db.models import Base, Message  # noqa: E402
    from db.session import SessionLocal, engine  # noqa: E402

    Base.metadata.create_all(bind=engine)

    conn = raw_conn(PROBE_DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            # 排除 A 组留下的对照表 cmp_raw —— 它本来就是故意用 latin1 建的，
            # 不参与「ORM 建出来的表」这组断言。
            tables = sorted(r[0] for r in cur.fetchall() if r[0] != "cmp_raw")
        check("建出 6 张表", len(tables) == 6, f"{tables}")

        bad = []
        for t in tables:
            coll = table_collation(conn, PROBE_DB, t)
            if coll != "utf8mb4_0900_ai_ci":
                bad.append(f"{t}={coll or '?'}")
        check("6 张表 collation 全为 utf8mb4_0900_ai_ci（不受库默认值影响）", not bad, "; ".join(bad))

        # 直接对库复核一次连接字符集，确认连接层也是 utf8mb4。
        with conn.cursor() as cur:
            cur.execute("SELECT @@character_set_connection, @@collation_connection")
            ch, col = cur.fetchone()
        check("连接字符集 utf8mb4", str(ch) == "utf8mb4", f"{ch} / {col}")
    finally:
        conn.close()

    # ── C. 走 ORM 做一次 emoji 完整往返 ───────────────────────
    print("\n[C] 走 ORM 写入并读回 emoji（端到端往返）")
    db = SessionLocal()
    try:
        from db.models import Conversation, Model, User  # noqa: E402

        u = User(username="probe_user", email="probe@example.com", plan="free")
        m = Model(name="probe-model", provider="ollama", param_size="1B", context_window=2048)
        db.add_all([u, m])
        db.flush()
        conv = Conversation(user_id=u.id, model_id=m.id, title="字符集探针")
        db.add(conv)
        db.flush()
        msg = Message(conversation_id=conv.id, role="user", content=EMOJI, token_count=1)
        db.add(msg)
        db.commit()
        mid = msg.id
        db.expire_all()
        got = db.query(Message).filter(Message.id == mid).one().content
        check("emoji 经 ORM -> MySQL -> ORM 无损", got == EMOJI, repr(got))
    finally:
        db.close()

    # ── 清理 ─────────────────────────────────────────────────
    admin = raw_conn()
    try:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{PROBE_DB}`")
    finally:
        admin.close()
    print(f"\n[9] 已清理临时库 {PROBE_DB}")

    if _failures:
        print(f"\n结果：{len(_failures)} 项不符合预期 -> {_failures}")
        return 1
    print("\n结果：全部符合预期 —— 表字符集由代码声明决定，不依赖服务器默认值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
