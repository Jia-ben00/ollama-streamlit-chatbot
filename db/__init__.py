"""db 包：数据库 ORM 建模、连接池、建表脚本。"""

from db import models  # noqa: F401  # 保证 import db 时模型已注册
from db.models import Base  # noqa: F401
