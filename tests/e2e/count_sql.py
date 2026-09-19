"""N+1 实证脚本：用 SQLAlchemy 事件钩子数「不同写法各发了多少条 SQL」。

为什么值得留一个脚本：面试里说「我做了 N+1 优化」是空话，「同一份数据、同一个连接，
接口写法 1 条 SQL、naive 写法 23 条」才是证据。这个脚本就是生产这份证据的。

顺带对真实查询跑 EXPLAIN，把「走哪个索引、有没有回表」落到执行计划上。

前置：先跑 `tests/e2e/setup_db.py` 建好临时库和数据。凭据从环境变量读。

用法：
    set MYSQL_PASSWORD=你的密码
    python tests/e2e/count_sql.py
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

USER = os.getenv("MYSQL_USER", "root")
PASSWORD = os.getenv("MYSQL_PASSWORD", "")
HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
PORT = os.getenv("MYSQL_PORT", "3306")
DB = os.getenv("E2E_DB", "chatbot_api_e2e")


def main() -> int:
    if not PASSWORD:
        print("[FAIL] 没有设置 MYSQL_PASSWORD 环境变量。")
        return 2

    os.environ["DATABASE_URL"] = (
        f"mysql+pymysql://{USER}:{PASSWORD}@{HOST}:{PORT}/{DB}?charset=utf8mb4"
    )

    from sqlalchemy import event, text

    from api.routers.conversations import list_conversations
    from db.models import Conversation
    from db.session import SessionLocal, engine

    counter = {"n": 0}
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1
        statements.append(" ".join(statement.split()))

    db = SessionLocal()

    # ── A. 接口实现：显式 LEFT JOIN + COUNT ──────────────
    counter["n"] = 0
    statements.clear()
    result = list_conversations(user_id=1, db=db)
    print(f"A. 接口实现 list_conversations（返回 {len(result)} 个会话）发 SQL 条数 = {counter['n']}")
    for s in statements:
        print(f"     SQL: {s[:220]}")

    # ── B. naive 实现：靠 relationship 懒加载逐个查 ───────
    counter["n"] = 0
    statements.clear()
    convs = db.query(Conversation).filter(Conversation.user_id == 1).all()
    for c in convs:
        len(c.messages)  # 每访问一次就发一条 SQL —— 这就是 N+1
    print(f"B. naive 实现（{len(convs)} 个会话逐个取 c.messages）发 SQL 条数 = {counter['n']}")
    print(f"   结论：1 条查会话 + {len(convs)} 条查消息 = {1 + len(convs)} 条（N+1）")

    db.close()

    # ── C. EXPLAIN：真实查询的执行计划 ───────────────────
    conn = engine.connect()

    def explain(title: str, sql: str) -> None:
        print(f"C. EXPLAIN {title}")
        for row in conn.execute(text("EXPLAIN " + sql)).fetchall():
            m = row._mapping
            print(
                f"     table={m.get('table')} type={m.get('type')} key={m.get('key')} "
                f"rows={m.get('rows')} extra={m.get('Extra')}"
            )

    explain(
        "会话列表（LEFT JOIN messages + GROUP BY）",
        "SELECT c.id, COUNT(m.id) FROM conversations c "
        "LEFT JOIN messages m ON m.conversation_id = c.id "
        "WHERE c.user_id = 1 GROUP BY c.id",
    )
    explain(
        "消息列表游标分页（走索引，type=range）",
        "SELECT * FROM messages WHERE conversation_id = 1 AND id < 100 ORDER BY id DESC LIMIT 50",
    )
    explain(
        "时间范围查询（同一张表，索引却用不上：type=ALL + Using filesort）",
        "SELECT * FROM messages WHERE created_at >= '2026-01-01' ORDER BY created_at DESC",
    )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
