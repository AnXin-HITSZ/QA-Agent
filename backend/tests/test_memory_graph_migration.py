"""迁移 0009 与记忆图模型的一致性（**离线**，不连任何数据库）。

与 tests/test_memory_migration.py / test_metering_migration.py 同一套解析与比对
（共用 tests/migrationkit.py），这里负责记忆图的七张表：
memory_entities / memory_entity_aliases / memory_entity_mentions / memory_relations /
memory_events / memory_event_participants / memory_fact_links。

额外看护四点（这些保证靠列 / 键的存在与缺席承载，改表时最容易只改一边）：
- **同名 ≠ 同一实体**：memory_entities 的 (user_id, scope, normalized) 上不能有唯一键
  （重名合并是消歧决策，不是约束能代替的；唯一键会让同名新实体写不进来或撞已删行）；
- **未消歧可表示**：提及 / 参与者的 entity_id 必须可空 —— 候选模糊时保留待定，
  绝不强行绑定；
- **事实绑定有效版本**：memory_fact_links.memory_revision 必须 NOT NULL（绑定时的
  记忆版本是「正文更新后复核」的依据）；
- **正文列是 TEXT**：实体名 / 别名 / 提及 / 描述 / 参与者名称 / 摘录都是模型或用户
  产出，长度不可控（0002 迁移的 1406 教训）。
"""

from __future__ import annotations

from tests import migrationkit as mig

GRAPH_TABLES = (
    "memory_entities",
    "memory_entity_aliases",
    "memory_entity_mentions",
    "memory_relations",
    "memory_events",
    "memory_event_participants",
    "memory_fact_links",
)


def model_tables() -> dict[str, mig.Table]:
    return mig.model_tables("app.memory.graph.tables")


def _column(table: str, name: str) -> mig.Column:
    return next(c for c in model_tables()[table].columns if c.name == name)


def test_migration_matches_graph_models():
    """0009 建的表必须与 app/memory/graph/tables.py 逐项一致（列 / 类型 / 可空 / 主键 / 唯一键 / 索引）。"""
    model, migration = model_tables(), mig.migration_tables()
    missing = sorted(set(model) - set(migration))
    assert not missing, f"迁移里少了这些表:{missing}"
    for name, want in model.items():
        mig.compare_tables(migration[name], want, label=name)


def test_graph_tables_are_exactly_the_planned_ones():
    """七张表一张不多、一张不少（少了只会在投影 / 召回时才炸，太晚）。"""
    assert set(GRAPH_TABLES) == set(model_tables())
    assert set(GRAPH_TABLES) <= set(mig.migration_tables())


def test_entities_have_no_unique_key_on_the_normalized_name():
    """同名 ≠ 同一实体：规范名上**不能**有唯一键（含 (user_id, scope, normalized) 组合）。"""
    entity = model_tables()["memory_entities"]
    assert "normalized" not in entity.primary_key
    for name, cols in entity.uniques.items():
        assert "normalized" not in cols, f"memory_entities 的规范名上出现了唯一键:{name}{cols}"


def test_unresolved_entities_are_representable():
    """未消歧必须可表示：提及 / 参与者的 entity_id 可空（NULL = 待定 / 未绑定）。"""
    mentions = {c.name: c for c in model_tables()["memory_entity_mentions"].columns}
    participants = {c.name: c for c in model_tables()["memory_event_participants"].columns}
    assert mentions["entity_id"].nullable is True
    assert participants["entity_id"].nullable is True
    # 待定状态本身也要有落点（resolved / pending / unresolved 收敛在 status 列）
    assert mentions["status"].nullable is False


def test_fact_links_bind_the_valid_memory_version():
    """事实绑定必须带上「绑定时的有效记忆版本」，且幂等键覆盖（事实 × 记忆 × 轮次）。"""
    links = model_tables()["memory_fact_links"]
    cols = {c.name: c for c in links.columns}
    assert cols["memory_revision"].nullable is False
    assert links.uniques["uk_memory_fact_links"] == (
        "fact_kind", "fact_id", "memory_id", "turn_key")


def test_events_are_not_just_triples():
    """事件必须带:原始时间表述 / 锚点 / 可确定范围 + 精度 / 状态 / 有依据属性 / 去重摘要。"""
    cols = {c.name for c in model_tables()["memory_events"].columns}
    for name in ("time_expression", "anchor", "start_at", "end_at", "time_precision",
                 "status", "attributes", "normalized", "description"):
        assert name in cols, f"memory_events 缺少 {name}"


def test_graph_text_columns_are_text_not_capped_varchars():
    """正文类列是 TEXT:模型 / 用户产出的长度不可控,估窄了会在严格模式下整批 1406。"""
    for table, column in (
        ("memory_entities", "name"),
        ("memory_entity_aliases", "alias"),
        ("memory_entity_mentions", "surface"),
        ("memory_relations", "object_text"),
        ("memory_events", "description"),
        ("memory_event_participants", "name_text"),
        ("memory_fact_links", "quote"),
    ):
        col = _column(table, column)
        assert col.type.upper() in ("TEXT", "MEDIUMTEXT", "LONGTEXT"), \
            f"{table}.{column} 应当是 TEXT,而不是 {col.type}"


def test_graph_tables_carry_isolation_and_generation():
    """隔离(scope)与代次(generation)是硬前提:少一列,彻底清除后就可能被旧任务回写。"""
    for table in ("memory_entities", "memory_entity_mentions", "memory_relations",
                  "memory_events", "memory_fact_links"):
        cols = {c.name: c for c in model_tables()[table].columns}
        assert cols["scope"].nullable is False, f"{table}.scope 必须 NOT NULL"
        assert cols["generation"].nullable is False, f"{table}.generation 必须 NOT NULL"
