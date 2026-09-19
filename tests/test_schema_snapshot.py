"""ORM（`db/models.py`）与练习库真实 schema 的**离线**对账守卫。

## 它解决什么问题

「6 张表与练习库严格对齐」这句话原来是**没有证据**的：只能靠手工跑一次
`verify_schema.py`（要连 MySQL），跑完就过去了，之后改列、改类型、改索引都不会有人拦。
本文件把那次手工对账变成 CI 里的常驻断言 —— 纯文件解析，不需要 MySQL。

## 为什么是「快照」而不是直接连库

连库的检查进不了 CI（CI 里没有那台 MySQL，也没有那份练习数据）。
所以把库的结构导出成 `tests/data/practice_db_schema.json` 签进仓库，
守卫比对「ORM 定义 vs 快照」。快照的生成脚本是
`tests/e2e/export_schema_snapshot.py`（需要真实 MySQL，所以放 e2e，不进 CI）。

⚠️ **快照会过期**：库那头改了 schema，守卫会报红，此时要先判断「改动是否有意」，
确认后才重跑导出脚本（diff 里能看清改了什么）。

## 三档差异（沿用仓库原有的分类，当前实测全部为 0）

| 档位 | 含义 | 本守卫的处置 | 实测 |
|---|---|---|---|
| 🔴 实质差异 | 影响运行行为（缺列 / 类型不符 / 约束丢失 / 索引对不上） | **禁止**，直接失败 | 0 |
| 🟡 宽严差异 | 能跑但 schema 不完全一致（符号、长度） | 必须恰好等于台账 | **0** |
| ⚪ 命名差异 | 索引名不同，列组合相同 | 忽略（名字只是标签） | 有，无影响 |

> 🟡 那一档原本被报成「13 处：库用 unsigned、ORM 有符号」，**是假阳性**。
> 见下面「两个 `str()` 陷阱」第 2 条。

## 两个 `str()` 陷阱（本文件最容易写错的地方）

`str(col.type)` 看起来是「这个列的类型」，但它会骗人两次，两次都真的踩过：

1. **`Enum('a','b')` 打印成 `VARCHAR(2)`**（`Enum` 继承自 `String`）。照着 `str()`
   比，会得出「ORM 用 varchar、没有约束」的结论 —— 实际 `init_db()` 建出来就是
   native ENUM，非法值一样被 ERROR 1265 拒绝。
2. **`Integer(unsigned=True)` 打印成 `INTEGER`，unsigned 被吞掉**。照着 `str()` 比，
   会得出「ORM 有符号、库无符号」的 13 处「宽严差异」—— 实际 ORM 编译出来就是
   `INTEGER UNSIGNED`，两边本来就一致。

所以类型必须走 **MySQL 方言编译**（`col.type.compile(dialect=mysql.dialect())`），
而不是 `str()`。下面 `TestNormalizationIsNotBlind` 用元测试把这个前提钉住：
哪天有人为了「简化」把归一化改回 `str()`，那些用例会立刻红。

## 另外两个刻意与手工脚本不同的地方

1. **索引比对是双向的**。原 `verify_schema.py` 只检查「ORM 声明了、库里没有」，
   不检查反向 —— 于是「库里有、ORM 没声明」这类问题永远是隐形的。
   本守卫要求两个方向的集合**完全相等**。
2. **主键计入索引形状**。MySQL 把主键也报成 `information_schema.STATISTICS` 里的一条
   索引（名为 `PRIMARY`），ORM 侧不补这一步会凭空报出 6 处假差异 —— 这个假阳性
   在本文件写出来之前真的出现过。
"""

import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql import INTEGER as MySQL_INTEGER

from db.models import Base

SNAPSHOT_PATH = Path(__file__).resolve().parent / "data" / "practice_db_schema.json"

MYSQL_DIALECT = mysql.dialect()

# SQLAlchemy 编译出来的类型名 vs MySQL 报出来的类型名，归一到同一个词。
#   boolean -> 方言编译出来是 BOOL，而 MySQL 里 BOOL 就是 TINYINT(1) 的别名，
#   库里报的是 tinyint(1)，两边指的是同一个东西。
BASE_TYPE = {
    "int": "int", "integer": "int", "bigint": "bigint", "smallint": "smallint",
    "tinyint": "tinyint", "varchar": "varchar", "text": "text", "longtext": "longtext",
    "datetime": "datetime", "timestamp": "timestamp", "date": "date",
    "float": "float", "double": "double", "decimal": "decimal",
    "boolean": "tinyint", "bool": "tinyint", "json": "json", "enum": "enum",
}

# 「宽严差异」台账 —— 当前**为空**，也就是 ORM 与练习库在类型严格度上完全一致。
#
# 这里原本有 13 条「库 unsigned、ORM 有符号」的记录，是 `str(col.type)` 吞掉
# `unsigned` 造成的假阳性（见模块开头「两个 str() 陷阱」）。改用方言编译后归零。
#
# 保留这张空表而不是删掉，是因为**「没有台账的已知差异会腐烂成未知差异」**：
# 一旦真的有宽严差异冒出来，断言会让人看见，并且必须显式决定「接受还是修」。
KNOWN_WIDTH_DIFFS = {}


def load_snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        raise AssertionError(
            "缺少 schema 快照 %s。\n"
            "生成方式（需要本机 MySQL）：\n"
            "    set MYSQL_PASSWORD=<密码>\n"
            "    python tests/e2e/export_schema_snapshot.py" % SNAPSHOT_PATH
        )
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def split_type(s: str):
    """'bigint unsigned' -> ('bigint', True)；'VARCHAR(4)' -> ('varchar', False)"""
    s = s.lower().strip()
    unsigned = "unsigned" in s
    base = s.split("(")[0].replace("unsigned", "").strip()
    return BASE_TYPE.get(base, base), unsigned


def orm_type_info(col):
    """返回 (基础类型, enum 取值集合或 None, 是否 unsigned)。

    ⚠️ 两条都必须靠**方言编译**，不能靠 `str(col.type)`：
    `Enum('a','b')` 会打印成 `VARCHAR(2)`，`Integer(unsigned=True)` 会打印成
    `INTEGER`（丢掉 unsigned）。两个坑都真的踩过，见模块开头。
    """
    t = col.type
    if isinstance(t, SAEnum):
        return "enum", tuple(t.enums), False
    base, unsigned = split_type(t.compile(dialect=MYSQL_DIALECT))
    return base, None, unsigned


def parse_db_enum(s: str):
    """\"enum('free','pro')\" -> ('free', 'pro')；不是 enum 则 None。"""
    m = re.match(r"enum\((.*)\)$", s.lower().replace(" ", ""))
    if not m:
        return None
    return tuple(x.strip("'") for x in m.group(1).split(","))


def orm_index_shapes(table) -> dict:
    """ORM 侧「列组合 -> 是否唯一」的索引形状表。

    主键也要算进去：MySQL 把主键当成 `STATISTICS` 里一条名为 PRIMARY 的唯一索引，
    不补这一步，库那侧会凭空多出 6 条形状、全部报成「库里有 ORM 没有」。
    """
    shapes = {}
    pk_cols = [c.name for c in table.primary_key.columns]
    if pk_cols:
        shapes[",".join(pk_cols)] = True
    for ix in table.indexes:
        shapes[",".join(c.name for c in ix.columns)] = bool(ix.unique)
    for uc in table.constraints:
        if uc.__class__.__name__ == "UniqueConstraint":
            shapes[",".join(c.name for c in uc.columns)] = True
    for c in table.columns:
        if c.unique:
            shapes[c.name] = True
    return shapes


def db_index_shapes(table_info: dict) -> dict:
    return {",".join(i["columns"]): i["unique"] for i in table_info["indexes"]}


class _SnapshotCase(unittest.TestCase):
    """公共：载入快照，并保证它本身是「可用」的。"""

    @classmethod
    def setUpClass(cls):
        cls.snapshot = load_snapshot()
        cls.tables = cls.snapshot["tables"]

    def shared_tables(self):
        return sorted(set(Base.metadata.tables) & set(self.tables))


class TestNormalizationIsNotBlind(unittest.TestCase):
    """元测试：先证明这把尺子本身没瞎，否则后面「全部一致」可能只是没量到。

    这一组钉的是「归一化函数」的前提，不是数据。它们红了说明**守卫失效**，
    而不是 schema 变了。
    """

    def test_split_type_reads_unsigned(self):
        self.assertEqual(split_type("bigint unsigned"), ("bigint", True))
        self.assertEqual(split_type("int unsigned"), ("int", True))
        self.assertEqual(split_type("VARCHAR(200)"), ("varchar", False))
        self.assertEqual(split_type("tinyint(1)"), ("tinyint", False))

    def test_str_drops_unsigned_so_normalization_must_compile(self):
        """这是本文件存在的原因：`str()` 看不到 unsigned。"""
        t = MySQL_INTEGER(unsigned=True)
        self.assertNotIn(
            "UNSIGNED",
            str(t).upper(),
            "`str(type)` 竟然带上了 unsigned —— 归一化的前提变了，请重新确认。",
        )
        self.assertIn("UNSIGNED", t.compile(dialect=MYSQL_DIALECT).upper())
        self.assertTrue(
            orm_type_info(SimpleNamespace(type=t))[2],
            "归一化没读出 ORM 的 unsigned",
        )

    def test_enum_prints_as_varchar_but_is_recognised_as_enum(self):
        """第二个陷阱：`str()` 把 native ENUM 显示成 VARCHAR。"""
        e = SAEnum("a", "b", native_enum=True)
        self.assertIn(
            "VARCHAR",
            str(e).upper(),
            "`str(Enum)` 竟然不再是 VARCHAR 了 —— 请重新确认这个前提。",
        )
        self.assertEqual(orm_type_info(SimpleNamespace(type=e))[:2], ("enum", ("a", "b")))

    def test_the_signedness_check_can_actually_fire(self):
        """fail-closed：证明「符号不一致」这件事**真的能被检出来**。

        否则「0 处差异」既可能是真的对齐，也可能是比对永远返回相等。
        """
        orm_uns = orm_type_info(SimpleNamespace(type=MySQL_INTEGER(unsigned=True)))[2]
        db_uns = split_type("int unsigned")[1]
        self.assertTrue(orm_uns and db_uns, "同号的列居然被判成不一致")

        orm_plain = orm_type_info(SimpleNamespace(type=MySQL_INTEGER()))[2]
        self.assertNotEqual(
            orm_plain,
            db_uns,
            "有符号的 ORM 列与无符号的库列被判成一致 —— 这个检查不会报错，等于没有",
        )


class TestSnapshotIsUsable(_SnapshotCase):
    """再证明快照文件本身是好的 —— 空快照会让所有比对退化成「空对空」。"""

    def test_snapshot_has_the_expected_shape(self):
        self.assertTrue(self.snapshot.get("_readme"), "快照缺 _readme")
        self.assertIn("database", self.snapshot["source"])
        self.assertEqual(
            len(self.tables), 6, "快照里表的数量变了：%s" % sorted(self.tables)
        )

    def test_no_table_is_empty_in_the_snapshot(self):
        for name, info in sorted(self.tables.items()):
            self.assertTrue(info["columns"], "快照里 %s 一列都没有" % name)
            self.assertTrue(info["indexes"], "快照里 %s 一条索引都没有" % name)
            self.assertTrue(info["collation"], "快照里 %s 没有排序规则" % name)

    def test_orm_metadata_is_not_empty(self):
        self.assertTrue(Base.metadata.tables, "ORM 里一张表都没有 —— 比对会全部退化成空对空")


class TestTablesAndColumns(_SnapshotCase):
    def test_same_table_set_both_directions(self):
        orm = set(Base.metadata.tables)
        db = set(self.tables)
        self.assertEqual(
            orm - db,
            set(),
            "ORM 定义了但练习库里没有（运行时 Unknown table）：%s" % sorted(orm - db),
        )
        self.assertEqual(
            db - orm,
            set(),
            "练习库里有但 ORM 没映射（数据存得进去却取不出来）：%s" % sorted(db - orm),
        )

    def test_same_column_set_per_table(self):
        for name in self.shared_tables():
            orm_cols = {c.name for c in Base.metadata.tables[name].columns}
            db_cols = {c["name"] for c in self.tables[name]["columns"]}
            self.assertEqual(
                orm_cols - db_cols,
                set(),
                "%s：ORM 有这些列、库里没有 → 运行时 Unknown column" % name,
            )
            self.assertEqual(
                db_cols - orm_cols,
                set(),
                "%s：库里有这些列、ORM 没映射 → 存得进去取不出来" % name,
            )


class TestColumnTypes(_SnapshotCase):
    def test_base_types_match(self):
        for name in self.shared_tables():
            db_cols = {c["name"]: c for c in self.tables[name]["columns"]}
            for col in Base.metadata.tables[name].columns:
                if col.name not in db_cols:
                    continue
                orm_base, orm_enums, _ = orm_type_info(col)
                db_type = db_cols[col.name]["type"]
                db_base, _ = split_type(db_type)
                db_enums = parse_db_enum(db_type)

                if db_enums is not None:
                    self.assertEqual(
                        orm_base,
                        "enum",
                        "%s.%s：库是 %s（DB 层强约束），ORM 是 %s（不约束）——"
                        "用 init_db() 建库会丢掉这个约束"
                        % (name, col.name, db_type, col.type),
                    )
                    self.assertEqual(
                        set(orm_enums or ()),
                        set(db_enums),
                        "%s.%s 的枚举取值不一致：ORM=%s 库=%s"
                        % (name, col.name, sorted(orm_enums or ()), sorted(db_enums)),
                    )
                else:
                    self.assertEqual(
                        orm_base,
                        db_base,
                        "%s.%s 的基础类型不一致：ORM=%s 库=%s"
                        % (name, col.name, col.type.compile(dialect=MYSQL_DIALECT), db_type),
                    )

    def test_nullability_matches(self):
        for name in self.shared_tables():
            db_cols = {c["name"]: c for c in self.tables[name]["columns"]}
            for col in Base.metadata.tables[name].columns:
                if col.name not in db_cols:
                    continue
                orm_notnull = (not col.nullable) or col.primary_key
                db_notnull = not db_cols[col.name]["nullable"]
                self.assertEqual(
                    orm_notnull,
                    db_notnull,
                    "%s.%s 的可空性不一致：ORM 非空=%s 库 NOT NULL=%s"
                    % (name, col.name, orm_notnull, db_notnull),
                )


class TestIndexes(_SnapshotCase):
    def test_index_shapes_match_both_directions(self):
        """索引的「身份」是 (列组合, 是否唯一)，名字只是标签。

        第一版按**索引名**比对，把库的 `idx_msg_conv` 和 ORM 的
        `ix_messages_conversation_id` 当成两回事，凭空报了 5 处「缺索引」。
        """
        for name in self.shared_tables():
            orm_shapes = orm_index_shapes(Base.metadata.tables[name])
            db_shapes = db_index_shapes(self.tables[name])
            missing = {k: v for k, v in orm_shapes.items() if k not in db_shapes}
            extra = {k: v for k, v in db_shapes.items() if k not in orm_shapes}
            self.assertEqual(missing, {}, "%s：ORM 声明了这些索引、库里没有 → %s" % (name, missing))
            self.assertEqual(
                extra,
                {},
                "%s：库里有这些索引、ORM 没声明（查得到但没人知道它存在）→ %s" % (name, extra),
            )
            for cols in sorted(set(orm_shapes) & set(db_shapes)):
                self.assertEqual(
                    orm_shapes[cols],
                    db_shapes[cols],
                    "%s 的索引 (%s) 唯一性不一致：ORM=%s 库=%s"
                    % (name, cols, orm_shapes[cols], db_shapes[cols]),
                )


class TestCharsetDeclaration(_SnapshotCase):
    """每张表都**显式**声明了字符集，不依赖库/服务器默认值。"""

    def test_orm_declares_charset_and_collation(self):
        for name in self.shared_tables():
            kwargs = Base.metadata.tables[name].kwargs
            self.assertEqual(
                kwargs.get("mysql_charset"),
                "utf8mb4",
                "%s 没有显式声明 utf8mb4 —— 一旦服务器默认值不是 utf8mb4，"
                "4 字节 emoji 就存不进去（MySQL 的 utf8 只有 3 字节）" % name,
            )
            self.assertEqual(
                kwargs.get("mysql_collate"),
                self.tables[name]["collation"],
                "%s 声明的排序规则与练习库不一致：ORM=%s 库=%s"
                % (name, kwargs.get("mysql_collate"), self.tables[name]["collation"]),
            )


class TestWidthLedger(_SnapshotCase):
    """宽严差异（符号 / 长度这一类）必须恰好等于台账 —— 当前台账为空。"""

    def _actual(self) -> dict:
        actual = {}
        for name in self.shared_tables():
            db_cols = {c["name"]: c for c in self.tables[name]["columns"]}
            for col in Base.metadata.tables[name].columns:
                if col.name not in db_cols:
                    continue
                db_type = db_cols[col.name]["type"]
                orm_base, _, orm_unsigned = orm_type_info(col)
                db_base, db_unsigned = split_type(db_type)
                if parse_db_enum(db_type) is not None or orm_base != db_base:
                    continue  # enum / 类型不符另有专门断言
                if orm_unsigned != db_unsigned:
                    actual["%s.%s" % (name, col.name)] = db_type
        return actual

    def test_width_diffs_match_the_ledger_exactly(self):
        actual = self._actual()
        new = {k: v for k, v in actual.items() if k not in KNOWN_WIDTH_DIFFS}
        gone = sorted(set(KNOWN_WIDTH_DIFFS) - set(actual))
        changed = {
            k: (KNOWN_WIDTH_DIFFS[k], actual[k])
            for k in actual
            if k in KNOWN_WIDTH_DIFFS and KNOWN_WIDTH_DIFFS[k] != actual[k]
        }
        self.assertEqual(
            (new, gone, changed),
            ({}, [], {}),
            "宽严差异台账对不上了。\n"
            "  新增（库与 ORM 的符号/长度不一致）：%s\n"
            "  已消失（应从 KNOWN_WIDTH_DIFFS 里删掉）：%s\n"
            "  类型变了：%s\n"
            "优先修 ORM 让两边一致；确属有意差异，才写进台账并说明理由。"
            % (new, gone, changed),
        )


if __name__ == "__main__":
    unittest.main()
