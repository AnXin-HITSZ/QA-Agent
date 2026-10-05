"""迁移 SQL 与模型的一致性(**离线**,不连任何数据库)。

为什么值得单独测:迁移(backend/migrations/*.up.sql)是手写的,而模型(app/metering/tables.py)
才是「代码认为库长什么样」的唯一来源。两边一旦漂移,本地一切正常,直到在 ECS 上手工执行迁移
才炸。这里把 .sql 解析成结构(列名 / 类型 / 可空 / 自增 / 主键 / 唯一键 / 索引)再与模型逐项
比对,没有 MySQL 也能发现漂移。

为什么不逐字比对 DDL 文本:SQL 是人排版出来的(列对齐、UNIQUE KEY 写法、DEFAULT CHARSET),
与 SQLAlchemy 渲染的字符串注定对不上;比结构才能既保住保证,又不逼着人写机器格式。

本文件负责调用计量的四张表(0001 / 0002);认证与用户管理的六张表(0003)在
tests/test_auth_migration.py,两处共用 tests/migrationkit.py 的解析与比对。
"""

from __future__ import annotations

import re

from tests import migrationkit as mig

BACKEND = mig.BACKEND
TABLES = ("call_events", "call_event_items", "cache_events", "price_config")


def model_tables() -> dict[str, mig.Table]:
    return mig.model_tables("app.metering.tables")


def migration_tables() -> dict[str, mig.Table]:
    return mig.migration_tables()


# ---- 用例 ----


def test_migration_matches_models():
    """迁移建的库结构必须与模型一致(列 / 类型 / 可空 / 自增 / 主键 / 唯一键 / 索引,逐项)。"""
    model, migration = model_tables(), migration_tables()
    missing = sorted(set(model) - set(migration))
    assert not missing, f"迁移里少了这些表:{missing}"
    for name, want in model.items():
        mig.compare_tables(migration[name], want, label=name)


def test_migration_covers_every_planned_table():
    """四张表一张都不能少(漏一张只会等到线上写日志时才发现)。"""
    assert set(TABLES) == set(model_tables())
    assert set(TABLES) <= set(migration_tables())


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
    snapshots: list[dict[str, mig.Table]] = [{}]
    for item in ups:
        nxt = dict(snapshots[-1])
        mig.apply_up(nxt, mig.text(item.path), where=item.path.name)
        snapshots.append(nxt)

    downs = {(m.version, m.name): m for m in mig.migrations("down")}
    schema = dict(snapshots[-1])
    for i, up in enumerate(reversed(ups)):
        down = downs[(up.version, up.name)]
        mig.apply_down(schema, mig.text(down.path), where=down.path.name)
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
