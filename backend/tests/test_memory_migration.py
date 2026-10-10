"""迁移 0004 / 0005 与长期记忆模型的一致性（**离线**，不连任何数据库）。

与 tests/test_metering_migration.py / test_auth_migration.py 同一套解析与比对
（共用 tests/migrationkit.py），这里负责六张表：memory_items / memory_history /
memory_jobs / memory_user_state / memory_sources / memory_ops。

额外看护四点：
- **没有无主的表**：迁移里出现的每一张表都必须归某个模型模块所有（三处合并校验，
  防止两边只改一处）；
- **正文列必须够宽**：记忆正文是 TEXT（不按「常见长度」估成 VARCHAR 再撞 1406，
  0002 迁移吃过这个亏）；这里把列类型钉死，防止后来人顺手改窄；
- **0005 是增量迁移**：已执行 0004 的库要能直接升级（列只加不改名、键只加不删），
  回滚文件要能把结构倒回 0004（test_metering_migration 的逐版快照比对覆盖这一点）；
- **两张新表的主键形态**：memory_sources 用自增 BIGINT（同一记忆多个来源是常态），
  memory_ops 用 CHAR(32)（与 memory_jobs 一致的 uuid4().hex）。
"""

from __future__ import annotations

from tests import migrationkit as mig

MEMORY_TABLES = (
    "memory_items",
    "memory_history",
    "memory_jobs",
    "memory_user_state",
    "memory_sources",
    "memory_ops",
)

# 0005 新增的增量列：这些是「作用域隔离 / 认领凭证 / 版本化索引标记 / 来源与时间」的落点，
# 少了任何一列都会让对应的保证退回旧行为（且是静默的）—— 在这里钉死。
MIGRATION_0005_COLUMNS = {
    "memory_items": ("scope", "kind", "event_time", "generation", "meta_version",
                     "indexed_revision", "indexed_meta_version"),
    "memory_jobs": ("scope", "turn_id", "claim_token", "stages", "outcome", "committed_at"),
}


def model_tables() -> dict[str, mig.Table]:
    return mig.model_tables("app.memory.tables")


def _column(table: str, name: str) -> mig.Column:
    return next(c for c in model_tables()[table].columns if c.name == name)


def test_migration_matches_memory_models():
    """0004 / 0005 建的表必须与 app/memory/tables.py 逐项一致（列 / 类型 / 可空 / 主键 / 唯一键 / 索引）。"""
    model, migration = model_tables(), mig.migration_tables()
    missing = sorted(set(model) - set(migration))
    assert not missing, f"迁移里少了这些表:{missing}"
    for name, want in model.items():
        mig.compare_tables(migration[name], want, label=name)


def test_migration_matches_memory_models_for_0005():
    """0005 的增量列必须与模型逐项一致（列 / 类型 / 可空 / 键）—— 增量迁移最容易只改一处。"""
    model, migration = model_tables(), mig.migration_tables()
    for table, columns in MIGRATION_0005_COLUMNS.items():
        for name in columns:
            got = next((c for c in migration[table].columns if c.name == name), None)
            want = next((c for c in model[table].columns if c.name == name), None)
            assert want is not None, f"模型 {table} 里没有 {name}"
            assert got == want, f"{table}.{name}: 迁移 {got} ≠ 模型 {want}"


def test_memory_op_and_source_tables_have_the_isolation_columns():
    """隔离与可恢复清理靠的列：scope（隔离）、generation（清理范围）、claim_token（凭证）。"""
    model = model_tables()
    for table, name in (("memory_items", "scope"), ("memory_jobs", "scope"),
                        ("memory_sources", "scope"), ("memory_ops", "scope"),
                        ("memory_ops", "generation"), ("memory_ops", "claim_token"),
                        ("memory_jobs", "claim_token")):
        col = next((c for c in model[table].columns if c.name == name), None)
        assert col is not None, f"{table} 缺少隔离 / 凭证列 {name}"


def test_0006_isolates_history_state_and_job_identity():
    model = model_tables()
    assert model["memory_user_state"].primary_key == ("user_id", "scope")
    assert any(c.name == "scope" and not c.nullable for c in model["memory_history"].columns)
    assert model["memory_jobs"].uniques["uk_memory_jobs_dedupe"] == (
        "user_id", "kind", "scope", "dedupe_key")


def test_memory_tables_are_exactly_the_planned_ones():
    """六张表一张不多、一张不少（少了只会在线上写记忆时才发现）。"""
    assert set(MEMORY_TABLES) == set(model_tables())
    assert set(MEMORY_TABLES) <= set(mig.migration_tables())


def test_every_migration_table_is_owned_by_a_model_module():
    """迁移里的表集合 = 各模型模块（计量 / 认证 / 记忆 / 记忆图）的表并集：只改一处时立刻炸。"""
    owned = (set(mig.model_tables("app.metering.tables"))
             | set(mig.model_tables("app.auth.tables"))
             | set(model_tables())
             | set(mig.model_tables("app.memory.graph.tables")))
    assert owned == set(mig.migration_tables()), (
        f"迁移与模型模块的表集合不一致(模型 {sorted(owned)},迁移 {sorted(mig.migration_tables())})")


def test_memory_text_columns_are_text_not_capped_varchars():
    """正文列是 TEXT：不按「常见长度」估列宽 —— 记忆正文由模型产出，长度不可控，
    估窄了会在 MySQL 严格模式下整批 1406（0002 迁移的教训）。"""
    for table, column in (("memory_items", "text"), ("memory_history", "old_text"),
                          ("memory_history", "new_text")):
        col = _column(table, column)
        assert col.type.upper() in ("TEXT", "MEDIUMTEXT", "LONGTEXT"), \
            f"{table}.{column} 应当是 TEXT，而不是 {col.type}"


def test_memory_items_has_no_unique_constraint_on_the_hash():
    """content_hash 上**不能**有唯一键。

    软删的行留在表里；若 (user_id, content_hash) 唯一，用户删掉一条记忆后再看到
    同样的事实时就永远写不回来（唯一键撞上已删行）。去重必须在「有效记忆」集合上做
    （见 app/memory/repo.get_active_by_hash）。
    """
    uniques = model_tables()["memory_items"].uniques
    assert all("content_hash" not in cols for cols in uniques.values()), \
        f"memory_items 的 content_hash 上出现了唯一键:{uniques}"
