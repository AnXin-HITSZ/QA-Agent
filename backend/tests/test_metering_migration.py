"""迁移 SQL 与模型的一致性(**离线**,不连任何数据库)。

为什么值得单独测:迁移(backend/migrations/*.up.sql)是手写的,而模型(app/metering/tables.py)
才是「代码认为库长什么样」的唯一来源。两边一旦漂移,本地一切正常,直到在 ECS 上手工执行迁移
才炸。这里把 .sql 解析成结构(列名 / 类型 / 可空 / 自增 / 主键 / 唯一键 / 索引)再与模型逐项
比对,没有 MySQL 也能发现漂移。

为什么不逐字比对 DDL 文本:SQL 是人排版出来的(列对齐、UNIQUE KEY 写法、DEFAULT CHARSET),
与 SQLAlchemy 渲染的字符串注定对不上;比结构才能既保住保证,又不逼着人写机器格式。

解析覆盖两种语句并**按版本顺序重放**:`CREATE TABLE` 建表、`ALTER TABLE ... MODIFY` 改列
(0002 放宽 document_id / item_key 用的就是后者)。遇到第三种写法仍然显式报错,不悄悄漏检。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from sqlalchemy import UniqueConstraint, types as sa_types
from sqlalchemy.dialects import mysql

from tests import migrationkit as mig

BACKEND = mig.BACKEND
TABLES = ("call_events", "call_event_items", "cache_events", "price_config")


# ---- 结构:两边都归结成这几个数据类再比对 ----


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool
    auto_increment: bool


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
_AUTO_INCREMENT = re.compile(r"\bAUTO_INCREMENT\b", re.I)
_NOT_NULL = re.compile(r"\bNOT\s+NULL\b", re.I)
_NULL = re.compile(r"\bNULL\b", re.I)
_ENGINE = re.compile(r"ENGINE\s*=\s*(\w+)", re.I)
_CHARSET = re.compile(r"(?:DEFAULT\s+)?CHARSET\s*=\s*(\w+)", re.I)
_DROP_TABLE = re.compile(r"^DROP\s+TABLE\s+(IF\s+EXISTS\s+)?(\w+)$", re.I)
_ALTER_MODIFY = re.compile(r"^ALTER\s+TABLE\s+(\w+)\s+MODIFY\s+(?:COLUMN\s+)?(.+)$", re.I | re.S)


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
        "NULL / NOT NULL / AUTO_INCREMENT / COMMENT(刻意不写 DEFAULT:默认值全部在 Python 侧)")
    return Column(name=name, type=canon_type(type_sql), nullable=nullable,
                  auto_increment=auto_increment)


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
    for item in mig.migrations("up"):
        apply_up(out, mig.text(item.path), where=item.path.name)
    return out


def apply_up(schema: dict[str, Table], sql: str, *, where: str) -> None:
    """把一份 up 的语句作用到结构上:CREATE TABLE 建表、ALTER ... MODIFY 改列。"""
    for stmt in mig.statements(sql):
        if alter_modify(schema, stmt, where=where):
            continue
        table = parse_table(stmt)          # 其余写法在这里显式报错(宁可炸,不要漏检)
        assert table.name not in schema, f"{where}: 重复建表:{table.name}"
        schema[table.name] = table


def alter_modify(schema: dict[str, Table], stmt: str, *, where: str) -> bool:
    """识别并应用 `ALTER TABLE t MODIFY [COLUMN] 列名 类型 NULL|NOT NULL`;不是 ALTER 返回 False。"""
    m = _ALTER_MODIFY.match(stmt)
    if not m:
        return False
    name, spec = m.group(1), m.group(2).strip()
    table = schema.get(name)
    assert table is not None, f"{where}: ALTER 的表不在结构里:{name}"
    col = _column(spec, name)
    names = [c.name for c in table.columns]
    assert col.name in names, f"{where}: {name}.{col.name} 不在表里(改列不能改名)"
    schema[name] = replace(table, columns=tuple(col if c.name == col.name else c
                                               for c in table.columns))
    return True


def apply_down(schema: dict[str, Table], sql: str, *, where: str) -> None:
    """把一份 down 的语句作用到结构上:DROP TABLE IF EXISTS 删表、ALTER ... MODIFY 改回去。"""
    for stmt in mig.statements(sql):
        if alter_modify(schema, stmt, where=where):
            continue
        m = _DROP_TABLE.match(stmt)
        assert m and m.group(1), (
            f"{where}: down 只允许 DROP TABLE IF EXISTS / ALTER ... MODIFY:{stmt[:60]!r}")
        dropped = schema.pop(m.group(2), None)
        assert dropped is not None, f"{where}: 删了结构里没有的表:{m.group(2)}"


# ---- 模型 ----


def _auto_increment(table, column, impl) -> bool:
    """单列整数主键 = AUTO_INCREMENT;其余列一律不该有。"""
    return (column.primary_key and len(table.primary_key) == 1
            and isinstance(impl, sa_types.Integer) and column.autoincrement is not False)


def model_tables() -> dict[str, Table]:
    """模型渲染出的结构(类型按 MySQL 方言解析,与 .sql 用同一套规范化)。"""
    from app.metering.tables import Base

    dialect = mysql.dialect()
    out: dict[str, Table] = {}
    for table in Base.metadata.sorted_tables:
        columns = []
        for col in table.columns:
            impl = col.type.dialect_impl(dialect)
            columns.append(Column(name=col.name, type=canon_type(impl.compile(dialect=dialect)),
                                  nullable=bool(col.nullable),
                                  auto_increment=_auto_increment(table, col, impl)))
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


# ---- 用例 ----


def test_migration_matches_models():
    """迁移建的库结构必须与模型一致(列 / 类型 / 可空 / 自增 / 主键 / 唯一键 / 索引,逐项)。"""
    model, migration = model_tables(), migration_tables()
    assert set(migration) == set(model), (
        f"表集合不一致:模型 {sorted(model)},迁移 {sorted(migration)}")

    for name, want in model.items():
        got = migration[name]
        assert not got.foreign_keys, f"{name}: 迁移里出现了外键 —— 无外键是有意为之(方案 §4.5)"
        assert (got.engine, got.charset) == (want.engine, want.charset), \
            f"{name}: 表选项不符({got.engine} / {got.charset})"
        assert [c.name for c in got.columns] == [c.name for c in want.columns], (
            f"{name}: 列集合 / 顺序不一致(改表结构请新增一版迁移,不要改已有的):\n"
            f"模型:{[c.name for c in want.columns]}\n迁移:{[c.name for c in got.columns]}")
        for want_col, got_col in zip(want.columns, got.columns):
            assert got_col == want_col, f"{name}.{want_col.name}: 迁移 {got_col} ≠ 模型 {want_col}"
        assert got.primary_key == want.primary_key, \
            f"{name}: 主键不符(迁移 {got.primary_key} ≠ 模型 {want.primary_key})"
        assert got.uniques == want.uniques, \
            f"{name}: 唯一键不符(迁移 {got.uniques} ≠ 模型 {want.uniques})"
        assert got.indexes == want.indexes, \
            f"{name}: 索引不符(迁移 {got.indexes} ≠ 模型 {want.indexes})"


def test_migration_covers_every_planned_table():
    """四张表一张都不能少,也不能多出计划外的表:漏一张只会等到线上写日志时才发现。"""
    assert set(TABLES) == set(migration_tables()) == set(model_tables())


def test_files_are_paired_and_versioned():
    """<四位版本>_<名称> 成对出现、从 0001 起连续 —— 版本只增不复用(见 migrations/README.md)。"""
    ups, downs = mig.migrations("up"), mig.migrations("down")
    assert [m.version for m in ups] == list(range(1, len(ups) + 1)), \
        f"版本必须从 0001 起连续:{[m.version for m in ups]}"
    assert [(m.version, m.name) for m in downs] == [(m.version, m.name) for m in ups], \
        "每个 up 都要有同名 down(回滚要能执行)"


def test_down_files_revert_every_up():
    """回滚要能一步步真的倒回去:每执行一版 down,结构必须回到该版 up **之前**的样子。

    只断言「最后是空结构」不够 —— 表删掉就没了,down 里漏掉 ALTER(没把列宽改回去)照样会绿。
    所以逐版比对快照;全放完自然是空结构。down 里只许出现 DROP TABLE IF EXISTS 与
    ALTER ... MODIFY(线上回滚执行的就是这些文件)。
    """
    ups = mig.migrations("up")
    snapshots: list[dict[str, Table]] = [{}]
    for item in ups:
        nxt = dict(snapshots[-1])
        apply_up(nxt, mig.text(item.path), where=item.path.name)
        snapshots.append(nxt)

    downs = {(m.version, m.name): m for m in mig.migrations("down")}
    schema = dict(snapshots[-1])
    for i, up in enumerate(reversed(ups)):
        down = downs[(up.version, up.name)]
        apply_down(schema, mig.text(down.path), where=down.path.name)
        want = snapshots[len(ups) - i - 1]
        if schema != want:
            changed = sorted(set(schema) | set(want))
            diff = [name for name in changed if schema.get(name) != want.get(name)]
            raise AssertionError(
                f"{down.path.name} 回滚后结构没回到 {up.path.name} 之前的样子,差异表:{diff}")
    assert schema == {}


def test_app_never_creates_or_alters_tables():
    """应用启动不自动建表 / 改表(方案 §6):建表只走 migrations/ 下手写的 SQL。"""
    offenders: list[str] = []
    for path in (BACKEND / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "create_all" in text:
            offenders.append(f"{path.relative_to(BACKEND)}: create_all")
        if re.search(r"^\s*(?:from|import)\s+alembic\b", text, re.M):
            offenders.append(f"{path.relative_to(BACKEND)}: import alembic")
    assert offenders == [], f"应用代码里出现了自动 DDL / 迁移框架:{offenders}"


def test_migrations_are_plain_sql_only():
    """迁移目录里只有 .sql 与 README(不再引迁移框架);配置文件与依赖也要对得上。"""
    stray = sorted(item.relative_to(mig.MIGRATIONS_DIR).as_posix()
                   for item in mig.MIGRATIONS_DIR.rglob("*")
                   if item.is_file() and "__pycache__" not in item.parts
                   and item.suffix != ".sql" and item.name != "README.md")
    assert stray == [], f"migrations/ 下出现了非 SQL 文件:{stray}"
    assert not (BACKEND / "alembic.ini").exists(), "已改为纯 SQL 迁移,不再保留 alembic.ini"
    requirements = (BACKEND / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "alembic" not in requirements, "requirements 里不应再有迁移框架"
