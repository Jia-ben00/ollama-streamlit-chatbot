"""端到端联调的「起手式」：建临时库 → 用仓库的建表脚本真建表 → 灌种子数据。

设计要点：
- 建表走的是 `db/init_db.py`（`Base.metadata.create_all`），不是我另写一份 DDL。
  这样验证的是**交付的代码**，而不是「我手敲的 SQL 恰好能跑」。
- 用**独立的临时库**（默认 `chatbot_api_e2e`），不碰你本地的练习库。
  每次运行先 DROP 再 CREATE，保证从干净状态开始，结果可复现。

凭据一律从环境变量读，不写死在代码里：
    MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / E2E_DB

用法：
    set MYSQL_PASSWORD=你的密码        # Windows cmd
    export MYSQL_PASSWORD=你的密码     # bash
    python tests/e2e/setup_db.py
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
PORT = int(os.getenv("MYSQL_PORT", "3306"))
USER = os.getenv("MYSQL_USER", "root")
PASSWORD = os.getenv("MYSQL_PASSWORD", "")
DB = os.getenv("E2E_DB", "chatbot_api_e2e")

N_CONVERSATIONS = 20
MESSAGES_PER_CONVERSATION = 5


def main() -> int:
    if not PASSWORD:
        print("[FAIL] 没有设置 MYSQL_PASSWORD 环境变量。凭据不写进代码，请通过环境变量提供。")
        return 2

    # 必须在 import db.* 之前设置：db/session.py 在 import 时就读取 DATABASE_URL。
    os.environ["DATABASE_URL"] = (
        f"mysql+pymysql://{USER}:{PASSWORD}@{HOST}:{PORT}/{DB}?charset=utf8mb4"
    )

    import pymysql

    # ── 1. 重建临时库 ────────────────────────────────────
    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, charset="utf8mb4")
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{DB}`")
        cur.execute(f"CREATE DATABASE `{DB}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
    conn.commit()
    conn.close()
    print(f"[1] 临时库 {DB} 已重建（utf8mb4_0900_ai_ci）")

    # ── 2. 调用仓库的建表脚本 ─────────────────────────────
    from db.init_db import init_db  # noqa: E402

    init_db()

    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, database=DB, charset="utf8mb4")
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES")
        tables = sorted(r[0] for r in cur.fetchall())
        cur.execute(
            "SELECT table_collation FROM information_schema.tables WHERE table_schema=%s", (DB,)
        )
        collations = [r[0] or "" for r in cur.fetchall()]
    conn.close()

    ok_coll = all(c.startswith("utf8mb4") for c in collations)
    print(f"[2] 建表结果：{len(tables)} 张 -> {', '.join(tables)}")
    print(f"[2] collation 全部 utf8mb4：{ok_coll}")

    # ── 3. 灌种子数据 ────────────────────────────────────
    from db.models import Conversation, Message, Model, Tag, User  # noqa: E402
    from db.session import SessionLocal  # noqa: E402

    db = SessionLocal()
    try:
        user = User(username="alice", email="alice@example.com", plan="pro")
        db.add(user)
        model = Model(name="llama3.2", provider="ollama", param_size="3B", context_window=8192)
        db.add(model)
        db.flush()  # 拿到自增 id

        for i in range(1, N_CONVERSATIONS + 1):
            conv = Conversation(user_id=user.id, model_id=model.id, title=f"会话 {i}")
            db.add(conv)
            db.flush()
            for j in range(MESSAGES_PER_CONVERSATION):
                db.add(
                    Message(
                        conversation_id=conv.id,
                        role="user" if j % 2 == 0 else "assistant",
                        content=f"会话{i}的第{j}句",
                        token_count=10,
                        latency_ms=None if j % 2 == 0 else 120,
                    )
                )
        db.add(Tag(name="测试标签"))
        db.commit()

        n_conv = db.query(Conversation).count()
        n_msg = db.query(Message).count()
    finally:
        db.close()

    print(f"[3] 种子数据：conversations={n_conv}, messages={n_msg}")
    print("\n下一步：")
    print(f"  python tests/e2e/fake_ollama.py          # 终端 A")
    print(f'  set DATABASE_URL=mysql+pymysql://{USER}:***@{HOST}:{PORT}/{DB}?charset=utf8mb4')
    print(f"  set OLLAMA_BASE_URL=http://127.0.0.1:11435")
    print(f"  uvicorn api.main:app --port 8000         # 终端 B")
    print(f"  python tests/e2e/smoke.py                # 终端 C")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
