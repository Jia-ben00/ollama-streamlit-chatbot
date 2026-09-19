"""建表脚本：根据 db/models.py 的 ORM 定义，在 MySQL 里创建 6 张表。

用法：
    python -m db.init_db

原理：SQLAlchemy 的 `Base.metadata.create_all(engine)` 会读取所有已 import 的
模型类，生成对应的 CREATE TABLE DDL。所以这里的关键是先 import models，
确保所有表都注册到 metadata 里，再调用 create_all。

它是幂等的：表已存在就跳过（默认 checkfirst=True），不会重复建、不会清数据。
所以这个脚本既能在空库上初始化，也能在已建库上安全重跑。
"""

from db.models import Base  # noqa: F401  # 触发所有模型注册
from db.session import engine

# 手动 import 一遍模型，避免某些环境（如被 pyinstaller 打包）下依赖解析顺序问题。
# 正常 import db.models 已经足够，这里显式列出是为了可读性 + 防止未来拆分模型文件。
from db import models  # noqa: F401


def init_db() -> None:
    """创建所有表（已存在的跳过）。"""
    Base.metadata.create_all(bind=engine)
    print("已根据 ORM 模型创建表（已存在则跳过）。")
    print("表清单：", ", ".join(sorted(Base.metadata.tables.keys())))


if __name__ == "__main__":
    init_db()
