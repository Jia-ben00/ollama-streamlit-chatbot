"""把练习库的真实 schema 导出成 JSON 快照，供离线守卫 tests/test_schema_snapshot.py 比对。

为什么要留这个脚本（而不是只把 JSON 塞进仓库）：
快照是**会过期**的。数据库那头改了 schema，快照不会自己更新，守卫就会开始报假红。
所以生成方式必须可复现、可审计——想更新快照就重跑本脚本，diff 里能看清到底改了什么。

这个脚本需要真实 MySQL，所以放 tests/e2e/（不匹配 test*.py，不进 CI）。
守卫本身是纯文件解析，能进 CI。

用法：
    set MYSQL_PASSWORD=<本机 MySQL 密码>
    python tests/e2e/export_schema_snapshot.py

可选环境变量：SNAPSHOT_DB（默认 chatbot）、MYSQL_USER / MYSQL_HOST / MYSQL_PORT。
只读 information_schema，不碰业务数据。
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "tests" / "data" / "practice_db_schema.json"

DB = os.getenv("SNAPSHOT_DB", "chatbot")
USER = os.getenv("MYSQL_USER", "root")
HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
PORT = os.getenv("MYSQL_PORT", "3306")
PASSWORD = os.getenv("MYSQL_PASSWORD")

if not PASSWORD:
    sys.exit(
        "缺少 MYSQL_PASSWORD。凭据一律走环境变量，不要写进代码或快照文件：\n"
        "    Windows:  set MYSQL_PASSWORD=你的密码\n"
        "    bash   :  export MYSQL_PASSWORD=你的密码"
    )

from sqlalchemy import create_engine, text  # noqa: E402

URL = "mysql+pymysql://%s:%s@%s:%s/%s?charset=utf8mb4" % (USER, PASSWORD, HOST, PORT, DB)
engine = create_engine(URL)

COLUMNS_SQL = text(
    "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, EXTRA "
    "FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t "
    "ORDER BY ORDINAL_POSITION"
)

INDEX_SQL = text(
    "SELECT INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS cols, NON_UNIQUE "
    "FROM information_schema.STATISTICS "
    "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t "
    "GROUP BY INDEX_NAME, NON_UNIQUE"
)

TABLES_SQL = text(
    "SELECT TABLE_NAME, TABLE_COLLATION, ENGINE FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = :db AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
)

snapshot = {
    "_readme": (
        "练习库 schema 快照，由 tests/e2e/export_schema_snapshot.py 导出，"
        "tests/test_schema_snapshot.py 消费。"
        "只含结构、不含任何数据；想更新就重跑导出脚本，diff 里能看到改了什么。"
        "如果库那边改了 schema 而快照没跟着更新，守卫会报红——那通常是**该更新快照**，"
        "但要先确认改动本身是有意的。"
    ),
    "source": {
        "database": DB,
        "host": HOST,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    },
    "tables": {},
}

with engine.connect() as conn:
    snapshot["source"]["server_version"] = conn.execute(
        text("SELECT VERSION()")
    ).scalar()

    for tname, collation, engine_name in conn.execute(TABLES_SQL, {"db": DB}).fetchall():
        columns = []
        for name, col_type, nullable, key, extra in conn.execute(
            COLUMNS_SQL, {"db": DB, "t": tname}
        ).fetchall():
            columns.append(
                {
                    "name": name,
                    "type": col_type,
                    "nullable": nullable == "YES",
                    "key": key or "",
                    "extra": (extra or "").lower(),
                }
            )

        indexes = []
        for ix_name, cols, non_unique in conn.execute(
            INDEX_SQL, {"db": DB, "t": tname}
        ).fetchall():
            indexes.append(
                {
                    "name": ix_name,
                    "columns": cols.split(","),
                    "unique": non_unique == 0,
                }
            )
        indexes.sort(key=lambda i: (i["columns"], not i["unique"]))

        snapshot["tables"][tname] = {
            "collation": collation,
            "engine": engine_name,
            "columns": columns,
            "indexes": indexes,
        }

OUT.parent.mkdir(parents=True, exist_ok=True)
# 显式 newline="\n"：Path.write_text 在 Windows 上会把 \n 翻成 \r\n，
# 同一份快照在 Windows 和 Linux 上导出就会字节不同，diff 里全是噪音。
# 显式写 LF 后，导出结果与平台无关。
with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")

print("已导出 %d 张表 -> %s" % (len(snapshot["tables"]), OUT.relative_to(REPO)))
for t, info in sorted(snapshot["tables"].items()):
    print(
        "  %-18s %2d 列 / %d 索引   %s   %s"
        % (t, len(info["columns"]), len(info["indexes"]), info["collation"], info["engine"])
    )
