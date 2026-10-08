"""迁移文件的公共读法:按版本列文件、把 .sql 拆成可逐条执行的语句、把 SQL 解析成结构。

迁移是 backend/migrations/*.up.sql / *.down.sql(纯 SQL、由人手工执行,约定见该目录 README)。
「SQL 结构 ↔ 模型」的一致性比对(parse_table / model_tables / apply_up / apply_down)也在这里,
因为现在有两组表要对:调用计量(app/metering/tables.py,见 test_metering_migration.py)与
认证用户(app/auth/tables.py,见 test_auth_migration.py)。两边用的必须是同一套解析与比对,
否则「迁移与模型一致」这句话在两组表上含义不同。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple

BACKEND = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = BACKEND / "migrations"

# 与 D:\Projects\personal-site\backend\migrations 相同的约定:
# <四位版本>_<名称>.up.sql / .down.sql,版本只增不复用。
_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.(up|down)\.sql$")


class Migration(NamedTuple):
    version: int
    name: str
    path: Path


def migrations(direction: str) -> list[Migration]:
    """按版本升序列出某个方向的迁移文件;文件名不合规 / 版本重复直接报错。"""
    assert direction in ("up", "down"), direction
    out: list[Migration] = []
    for path in MIGRATIONS_DIR.glob(f"*.{direction}.sql"):
        m = _FILENAME.match(path.name)
        assert m, f"迁移文件名不合规:{path.name}(应为 <四位版本>_<名称>.{direction}.sql)"
        out.append(Migration(int(m.group(1)), m.group(2), path))
    out.sort(key=lambda item: item.version)
    versions = [item.version for item in out]
    assert len(versions) == len(set(versions)), f"版本号重复:{versions}"
    return out


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def statements(sql: str) -> list[str]:
    """把一份 .sql 拆成可逐条执行的语句。

    - 先去掉 `--` 行注释(不在字符串字面量里的);
    - 再按分号切,但跳过字符串字面量里的分号 —— 列注释里就有「用量;NULL = 未取得」;
    - 空白语句丢弃;末尾没带分号的最后一条也算一条(本仓库的文件都带分号,这里只是兜底)。

    本仓库的 SQL 不使用反斜杠转义(没有 \\' 之类):真写了会解析错并在比对时显式报错,
    不会静默切错。
    """
    body = _strip_line_comments(sql)
    out: list[str] = []
    buf: list[str] = []
    in_quote = False
    for ch in body:
        if ch == "'":
            in_quote = not in_quote          # '' 转义会翻转两次,自动还原
        elif ch == ";" and not in_quote:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _strip_line_comments(sql: str) -> str:
    """去掉 `-- 到行尾` 的注释(字符串字面量里的 `--` 原样保留),换行保留。"""
    out: list[str] = []
    in_quote = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            in_quote = not in_quote
            out.append(ch)
            i += 1
            continue
        if not in_quote and ch == "-" and sql.startswith("--", i):
            newline = sql.find("\n", i)
            if newline < 0:
                break
            out.append("\n")
            i = newline + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---- 结构:SQL 与模型两边都归结成这几个数据类再比对 ----


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool
    auto_increment: bool
    collation: str | None = None


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    uniques: dict[str, tuple[str, ...]]
    indexes: dict[str, tuple[str, ...]]
    foreign_keys: tuple[str, ...]
    engine: str
    charset: str


# ---- 解析 .sql ----


_TYPE_ALIASES = {"INTEGER": "INT", "NUMERIC": "DECIMAL"}

_PK = re.compile(r"^PRIMARY\s+KEY\s*\(([^)]*)\)$", re.I)
_UNIQUE_KEY = re.compile(r"^UNIQUE\s+KEY\s+(\w+)\s*\(([^)]*)\)$", re.I)
_UNIQUE_CONSTRAINT = re.compile(r"^CONSTRAINT\s+(\w+)\s+UNIQUE\s*\(([^)]*)\)$", re.I)
_INDEX = re.compile(r"^KEY\s+(\w+)\s*\(([^)]*)\)$", re.I)
_FOREIGN_KEY = re.compile(r"^(?:CONSTRAINT\s+\w+\s+)?FOREIGN\s+KEY\s*\(([^)]*)\)", re.I)
_COLUMN = re.compile(r"^(\w+)\s+([A-Za-z]+(?:\s*\([^)]*\))?)\s*(.*)$")
_COMMENT = re.compile(r"COMMENT\s+('(?:[^']|'')*')", re.I)
_COLLATE = re.compile(r"\bCOLLATE\s+\"?(\w+)\"?", re.I)
_AUTO_INCREMENT = re.compile(r"\bAUTO_INCREMENT\b", re.I)
_NOT_NULL = re.compile(r"\bNOT\s+NULL\b", re.I)
_NULL = re.compile(r"\bNULL\b", re.I)
_ENGINE = re.compile(r"ENGINE\s*=\s*(\w+)", re.I)
_CHARSET = re.compile(r"(?:DEFAULT\s+)?CHARSET\s*=\s*(\w+)", re.I)
_DROP_TABLE = re.compile(r"^DROP\s+TABLE\s+(IF\s+EXISTS\s+)?(\w+)$", re.I)
# 0005 起出现「加列 / 加键」的增量迁移（既有库必须能靠新版升级），解析随之扩展：
_ALTER_TABLE = re.compile(r"^ALTER\s+TABLE\s+(\w+)\s+(.+)$", re.I | re.S)
_ALTER_MODIFY_ACTION = re.compile(r"^MODIFY\s+(?:COLUMN\s+)?(.+)$", re.I | re.S)
_ADD_COLUMN = re.compile(r"^ADD\s+(?:COLUMN\s+)?(.+)$", re.I | re.S)
_ADD_UNIQUE = re.compile(r"^ADD\s+UNIQUE\s+KEY\s+(\w+)\s*\(([^)]*)\)$", re.I)
_ADD_INDEX = re.compile(r"^ADD\s+(?:KEY|INDEX)\s+(\w+)\s*\(([^)]*)\)$", re.I)
_DROP_COLUMN = re.compile(r"^DROP\s+(?:COLUMN\s+)?(\w+)$", re.I)
_DROP_INDEX = re.compile(r"^DROP\s+(?:KEY|INDEX)\s+(\w+)$", re.I)
# 回填用的 DML（ALTER 之后给存量行一个值）：不改结构，解析时跳过。
_DATA_ONLY = re.compile(r"^(?:UPDATE|DELETE|INSERT)\s+\w+\b", re.I)


def canon_type(sql: str) -> str:
    """类型规范化:去掉空白、统一同义词 —— NUMERIC(18, 8) == DECIMAL(18,8),INTEGER == INT。"""
    compact = re.sub(r"\s+", "", sql).upper()
    base, paren, rest = compact.partition("(")
    base = _TYPE_ALIASES.get(base, base)
    return f"{base}({rest}" if paren else base


def _column(item: str, table: str) -> Column:
    m = _COLUMN.match(item)
    assert m, f"{table}: 解析不了的条目:{item!r}"
    name, type_sql, rest = m.group(1), m.group(2), m.group(3)

    cm = _COMMENT.search(rest)
    comment = cm.group(1)[1:-1].replace("''", "'") if cm else None
    rest = _COMMENT.sub(" ", rest)
    assert comment is None or comment.strip(), f"{table}.{name}: COMMENT 是空的"

    # 列级排序规则(只有 users.email 用):显式写出的比较规则,属于列定义的一部分。
    colm = _COLLATE.search(rest)
    collation = colm.group(1).lower() if colm else None
    rest = _COLLATE.sub(" ", rest)

    auto_increment = bool(_AUTO_INCREMENT.search(rest))
    rest = _AUTO_INCREMENT.sub(" ", rest)          # 先剥完再找 NULL,免得 NULL 匹配到 NOT NULL 里
    if _NOT_NULL.search(rest):
        nullable = False
        rest = _NOT_NULL.sub(" ", rest)
    else:
        assert _NULL.search(rest), f"{table}.{name}: 没有明确写 NULL / NOT NULL"
        nullable = True
        rest = _NULL.sub(" ", rest)

    leftover = rest.strip(" ,")
    assert not leftover, (
        f"{table}.{name}: 解析不了的列修饰 {leftover!r} —— 本仓库的迁移只用 "
        "NULL / NOT NULL / AUTO_INCREMENT / COMMENT / COLLATE(刻意不写 DEFAULT:默认值全部在 Python 侧)")
    return Column(name=name, type=canon_type(type_sql), nullable=nullable,
                  auto_increment=auto_increment, collation=collation)


def _split_tail(stmt: str, open_at: int) -> tuple[str, str]:
    """返回 CREATE TABLE 第一个括号里的表体与收尾部分(引号里的括号不算)。"""
    depth, in_quote = 0, False
    for i in range(open_at, len(stmt)):
        ch = stmt[i]
        if ch == "'":
            in_quote = not in_quote                # '' 转义翻转两次,自动还原
        elif not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return stmt[open_at + 1:i], stmt[i + 1:]
    raise AssertionError(f"括号不配对:{stmt[:80]!r}")


def _split_items(body: str) -> list[str]:
    """按顶层逗号切表体:括号里的逗号(DECIMAL(18,8) / 键的列清单)与注释串里的逗号不算。"""
    out: list[str] = []
    buf: list[str] = []
    depth, in_quote = 0, False
    for ch in body:
        if ch == "'":
            in_quote = not in_quote
        if not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                out.append("".join(buf))
                buf = []
                continue
        buf.append(ch)
    out.append("".join(buf))
    return [item.strip() for item in out if item.strip()]


def _cols(spec: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in spec.split(",") if part.strip())


def parse_table(stmt: str) -> Table:
    """一条 CREATE TABLE 语句 → 结构。认不出的写法一律报错(宁可炸,不要漏检)。"""
    m = re.match(r"^CREATE\s+TABLE\s+(\w+)\s*\(", stmt, re.I)
    assert m, (f"本用例只解析 CREATE TABLE,遇到别的语句:{stmt[:60]!r}"
               "(新增迁移若用 ALTER / CREATE INDEX,请先扩展本用例的解析)")
    name = m.group(1)

    body, trailer = _split_tail(stmt, m.end() - 1)
    columns: list[Column] = []
    primary_key: tuple[str, ...] = ()
    uniques: dict[str, tuple[str, ...]] = {}
    indexes: dict[str, tuple[str, ...]] = {}
    foreign_keys: list[str] = []
    for item in _split_items(body):
        if mm := _PK.match(item):
            primary_key = _cols(mm.group(1))
            continue
        if mm := (_UNIQUE_KEY.match(item) or _UNIQUE_CONSTRAINT.match(item)):
            uniques[mm.group(1)] = _cols(mm.group(2))
            continue
        if mm := _INDEX.match(item):
            indexes[mm.group(1)] = _cols(mm.group(2))
            continue
        if mm := _FOREIGN_KEY.match(item):
            foreign_keys.append(_cols(mm.group(1)))
            continue
        columns.append(_column(item, name))

    engine, charset = _ENGINE.search(trailer), _CHARSET.search(trailer)
    assert engine and charset, f"{name}: 表尾缺少 ENGINE / CHARSET:{trailer.strip()!r}"
    return Table(name=name, columns=tuple(columns), primary_key=primary_key, uniques=uniques,
                 indexes=indexes, foreign_keys=tuple(foreign_keys),
                 engine=engine.group(1), charset=charset.group(1))


def migration_tables() -> dict[str, Table]:
    """按版本顺序把全部 up 文件作用一遍(只解析,不连库),得到「迁移认为库长什么样」。"""
    out: dict[str, Table] = {}
    for item in migrations("up"):
        apply_up(out, text(item.path), where=item.path.name)
    return out


def apply_up(schema: dict[str, Table], sql: str, *, where: str) -> None:
    """把一份 up 的语句作用到结构上:CREATE TABLE 建表、ALTER TABLE 改结构、回填 DML 跳过。"""
    for stmt in statements(sql):
        if _DATA_ONLY.match(stmt):
            continue                       # 给存量行回填值的 UPDATE / 清理用的 DELETE:不改结构
        if alter_table(schema, stmt, where=where):
            continue
        table = parse_table(stmt)          # 其余写法在这里显式报错(宁可炸,不要漏检)
        assert table.name not in schema, f"{where}: 重复建表:{table.name}"
        schema[table.name] = table


def alter_table(schema: dict[str, Table], stmt: str, *, where: str) -> bool:
    """识别并应用 `ALTER TABLE t <动作列表>`;不是 ALTER 返回 False。

    支持的动作（只覆盖本仓库用到的写法，其余一律报错）：
      MODIFY [COLUMN] 列 类型 NULL|NOT NULL      —— 改列（唯一的「就地改」）
      ADD [COLUMN] 列 类型 NULL|NOT NULL         —— 加列（必须是显式 NULL / NOT NULL）
      ADD [UNIQUE] KEY 名 (列…)                  —— 加键
      DROP [COLUMN] 列 / DROP [KEY|INDEX] 名     —— 删列 / 删键（回滚用）

    动作列表按**顶层逗号**切分：列注释里的逗号、类型括号里的逗号都不算（_split_items 负责）。
    0001~0004 的单列 MODIFY 与 0005 起的多动作 ALTER 走同一条路径 —— 两条路各写一份解析，
    「迁移与模型一致」这句话在两种写法上就会有不同的含义。
    """
    m = _ALTER_TABLE.match(stmt)
    if not m:
        return False
    name, body = m.group(1), m.group(2).strip()
    table = schema.get(name)
    assert table is not None, f"{where}: ALTER 的表不在结构里:{name}"
    for action in _split_items(body):
        if re.fullmatch(r"DROP\s+PRIMARY\s+KEY", action, re.I):
            table = replace(table, primary_key=())
            continue
        if mm := re.fullmatch(r"ADD\s+PRIMARY\s+KEY\s*\(([^)]+)\)", action, re.I):
            table = replace(table, primary_key=_cols(mm.group(1)))
            continue
        if mm := _ALTER_MODIFY_ACTION.match(action):
            col = _column(mm.group(1).strip(), name)
            names = [c.name for c in table.columns]
            assert col.name in names, f"{where}: {name}.{col.name} 不在表里(改列不能改名)"
            table = replace(table, columns=tuple(col if c.name == col.name else c
                                                 for c in table.columns))
            continue
        if mm := _ADD_UNIQUE.match(action):
            assert mm.group(1) not in table.uniques, f"{where}: {name} 重复的唯一键:{mm.group(1)}"
            table = replace(table, uniques={**table.uniques, mm.group(1): _cols(mm.group(2))})
            continue
        if mm := _ADD_INDEX.match(action):
            assert mm.group(1) not in table.indexes, f"{where}: {name} 重复的索引:{mm.group(1)}"
            table = replace(table, indexes={**table.indexes, mm.group(1): _cols(mm.group(2))})
            continue
        if mm := _ADD_COLUMN.match(action):
            col = _column(mm.group(1).strip(), name)
            assert col.name not in [c.name for c in table.columns], \
                f"{where}: {name}.{col.name} 已经存在(加列不能撞名)"
            table = replace(table, columns=(*table.columns, col))
            continue
        if mm := _DROP_COLUMN.match(action):
            col = mm.group(1)
            assert col in [c.name for c in table.columns], f"{where}: {name}.{col} 不在表里"
            assert col not in table.primary_key, f"{where}: 不能删主键列 {name}.{col}"
            table = replace(table, columns=tuple(c for c in table.columns if c.name != col))
            continue
        if mm := _DROP_INDEX.match(action):
            key = mm.group(1)
            assert key in table.indexes or key in table.uniques, f"{where}: {name} 没有键:{key}"
            table = replace(table, indexes={k: v for k, v in table.indexes.items() if k != key},
                            uniques={k: v for k, v in table.uniques.items() if k != key})
            continue
        raise AssertionError(f"{where}: 认不出的 ALTER 动作:{action!r}")
    schema[name] = table
    return True


def alter_modify(schema: dict[str, Table], stmt: str, *, where: str) -> bool:
    """单列 `ALTER TABLE t MODIFY [COLUMN] 列名 类型 NULL|NOT NULL`;不是这种返回 False。

    保留这个名字是因为它是「迁移读法」的一部分（apply_up / apply_down 走 alter_table，
    行为完全一致：MODIFY 只是动作列表里只有一项的特例）。
    """
    return alter_table(schema, stmt, where=where)


def apply_down(schema: dict[str, Table], sql: str, *, where: str) -> None:
    """把一份 down 的语句作用到结构上:DROP TABLE IF EXISTS 删表、ALTER TABLE 改回去。"""
    for stmt in statements(sql):
        if _DATA_ONLY.match(stmt):
            continue
        if alter_table(schema, stmt, where=where):
            continue
        m = _DROP_TABLE.match(stmt)
        assert m and m.group(1), (
            f"{where}: down 只允许 DROP TABLE IF EXISTS / ALTER TABLE(删列、删键、改列):"
            f"{stmt[:60]!r}")
        dropped = schema.pop(m.group(2), None)
        assert dropped is not None, f"{where}: 删了结构里没有的表:{m.group(2)}"


# ---- 模型 ----


def _auto_increment(table, column, impl) -> bool:
    """单列整数主键 = AUTO_INCREMENT;其余列一律不该有。"""
    from sqlalchemy import types as sa_types

    return (column.primary_key and len(table.primary_key) == 1
            and isinstance(impl, sa_types.Integer) and column.autoincrement is not False)


def model_tables(*modules: str) -> dict[str, Table]:
    """模型渲染出的结构(类型按 MySQL 方言解析,与 .sql 用同一套规范化)。

    每个模块提供自己的 `Base`(app.metering.tables / app.auth.tables);列级 COLLATE 从
    编译结果里拆出来单列一比 —— MySQL 方言渲染成 `COLLATE "utf8mb4_bin"`,与 SQL 里的
    写法只差引号,归一后比较。
    """
    from importlib import import_module

    from sqlalchemy import UniqueConstraint
    from sqlalchemy.dialects import mysql

    dialect = mysql.dialect()
    out: dict[str, Table] = {}
    for module in modules:
        tables = import_module(module).Base.metadata.sorted_tables
        for table in tables:
            columns = []
            for col in table.columns:
                impl = col.type.dialect_impl(dialect)
                rendered: str = impl.compile(dialect=dialect)
                cm = _COLLATE.search(rendered)
                if cm:
                    rendered = _COLLATE.sub(" ", rendered)
                columns.append(Column(
                    name=col.name,
                    type=canon_type(rendered),
                    nullable=bool(col.nullable),
                    auto_increment=_auto_increment(table, col, impl),
                    collation=cm.group(1).lower() if cm else None,
                ))
            assert table.name not in out, f"两个模型模块里出现了同名表:{table.name}"
            out[table.name] = Table(
                name=table.name,
                columns=tuple(columns),
                primary_key=tuple(c.name for c in table.primary_key),
                uniques={u.name: tuple(c.name for c in u.columns)
                         for u in table.constraints if isinstance(u, UniqueConstraint)},
                indexes={i.name: tuple(c.name for c in i.columns) for i in table.indexes},
                foreign_keys=(),
                engine="InnoDB",
                charset="utf8mb4",
            )
    return out


def compare_tables(got: Table, want: Table, *, label: str) -> None:
    """逐项比对一张表(迁移的 got vs 模型的 want);不一致直接抛 AssertionError。"""
    assert not got.foreign_keys, f"{label}: 迁移里出现了外键 —— 无外键是有意为之(方案 §4.5)"
    assert (got.engine, got.charset) == (want.engine, want.charset), \
        f"{label}: 表选项不符({got.engine} / {got.charset})"
    assert [c.name for c in got.columns] == [c.name for c in want.columns], (
        f"{label}: 列集合 / 顺序不一致(改表结构请新增一版迁移,不要改已有的):\n"
        f"模型:{[c.name for c in want.columns]}\n迁移:{[c.name for c in got.columns]}")
    for want_col, got_col in zip(want.columns, got.columns):
        assert got_col == want_col, f"{label}.{want_col.name}: 迁移 {got_col} ≠ 模型 {want_col}"
    assert got.primary_key == want.primary_key, \
        f"{label}: 主键不符(迁移 {got.primary_key} ≠ 模型 {want.primary_key})"
    assert got.uniques == want.uniques, \
        f"{label}: 唯一键不符(迁移 {got.uniques} ≠ 模型 {want.uniques})"
    assert got.indexes == want.indexes, \
        f"{label}: 索引不符(迁移 {got.indexes} ≠ 模型 {want.indexes})"
