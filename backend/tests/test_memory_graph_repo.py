"""记忆图 SQL 读写层（app/memory/graph/repo.py）在**真库**（临时 SQLite）上的行为。

覆盖七张表的关键语义（都是「错了会悄悄错」的那类）:
- **同名 ≠ 同一实体**:按名字 / 别名查找返回列表,两个「小明」各是各的,绝不合并;
- **幂等**:重复登记别名 / 提及 / 参与者 / 事实来源只算一次,且**不覆盖**已有消歧结论;
- **多来源**:一条事实可有多个来源（不同轮次）;解绑一条记忆不动事实本身;
- **版本**:内容真变了 revision 才 +1（对账据此发现投影过期）;
- **隔离与代次**:一切读写限定 user + scope;清除只动本用户本作用域。

MySQL 专有行为（真列宽 / 并发）不在这里假装通过,见 tests/test_memory_mysql.py 的同类约定。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.memory.graph import repo as graph
from app.memory.graph.models import (
    ENTITY_ACTIVE, ENTITY_DELETED, MENTION_PENDING, MENTION_RESOLVED,
)
from app.memory.models import content_hash

USER = "u-0000-0000-0000-000000000001"
OTHER = "u-0000-0000-0000-000000000002"
SCOPE = ""


def _now() -> datetime:
    return datetime(2026, 10, 10, 12, 0, 0)


@pytest.fixture
def graph_db(memory_db):
    """在 memory_db（临时 SQLite）上把记忆图的七张表也建出来（create_all 仅测试）。"""
    from app.memory.graph.tables import Base as GraphBase

    GraphBase.metadata.create_all(memory_db.db.get_engine())
    return memory_db


def _mk_entity(session, name="Caroline", *, kind="person", user=USER, scope=SCOPE) -> str:
    ent = graph.create_entity(session, user_id=user, scope=scope, kind=kind, name=name,
                              generation=1, now=_now())
    return ent.id


# ---- 实体:同名 ≠ 同一实体 ----


def test_same_name_is_not_the_same_entity(graph_db):
    """两个「小明」各建各的:查找返回 0..n 条,绝不因为名字一样就合并。"""
    with graph_db.db.session_scope() as session:
        a = _mk_entity(session, "小明")
        b = _mk_entity(session, "小明")
        assert a != b
        found = graph.find_entities_by_name(session, user_id=USER, scope=SCOPE, name="小明")
    assert sorted(e.id for e in found) == sorted([a, b])


def test_name_lookup_is_width_and_case_insensitive_but_keeps_original(graph_db):
    """匹配键做 NFKC + casefold:「 CAROLINE 」找得到「Caroline」,原文不动。"""
    with graph_db.db.session_scope() as session:
        _mk_entity(session, "Caroline")
        found = graph.find_entities_by_name(session, user_id=USER, scope=SCOPE, name="  caroline ")
        assert len(found) == 1
        assert found[0].name == "Caroline"       # 规范名保留原文,不被改写


def test_entity_scope_and_user_isolation(graph_db):
    """同名实体在不同用户 / 不同作用域下互不可见（隔离是数据层的事）。"""
    with graph_db.db.session_scope() as session:
        _mk_entity(session, "小明")
        _mk_entity(session, "小明", user=OTHER)
        _mk_entity(session, "小明", scope="eval:run-1")
        mine = graph.find_entities_by_name(session, user_id=USER, scope=SCOPE, name="小明")
        theirs = graph.find_entities_by_name(session, user_id=OTHER, scope=SCOPE, name="小明")
        evaled = graph.find_entities_by_name(session, user_id=USER, scope="eval:run-1", name="小明")
    assert len(mine) == len(theirs) == len(evaled) == 1
    assert mine[0].id != theirs[0].id != evaled[0].id != mine[0].id


def test_update_entity_bumps_revision_only_on_real_change(graph_db):
    with graph_db.db.session_scope() as session:
        ent = graph.create_entity(session, user_id=USER, scope=SCOPE, kind="person",
                                  name="Carol", generation=1, now=_now())
        assert graph.update_entity(session, entity_id=ent.id, now=_now(), name="Carol") is False
        assert graph.get_entity(session, ent.id).revision == 1
        assert graph.update_entity(session, entity_id=ent.id, now=_now(), kind="place") is True
        updated = graph.get_entity(session, ent.id)
    assert updated.kind == "place" and updated.revision == 2


def test_set_entity_status_soft_deletes_and_revives(graph_db):
    with graph_db.db.session_scope() as session:
        eid = _mk_entity(session)
        assert graph.set_entity_status(session, entity_id=eid, status=ENTITY_DELETED, now=_now())
        gone = graph.get_entity(session, eid)
        assert gone.status == ENTITY_DELETED and gone.deleted_at is not None
        assert graph.set_entity_status(session, entity_id=eid, status=ENTITY_ACTIVE, now=_now())
        back = graph.get_entity(session, eid)
    assert back.status == ENTITY_ACTIVE and back.deleted_at is None and back.revision == 3


# ---- 别名 ----


def test_alias_roundtrip_and_idempotency(graph_db):
    """别名能反查到实体;同一实体内重复登记是幂等的（撞唯一键不炸、不带倒本事务）。"""
    with graph_db.db.session_scope() as session:
        eid = _mk_entity(session)
        # 先写一条无关实体,验证幂等路径撞键时**不会**把它一起回滚
        other_id = _mk_entity(session, "Bob")
        assert graph.add_alias(session, entity_id=eid, user_id=USER, scope=SCOPE,
                               alias="Carol", origin="llm", now=_now()) is True
        assert graph.add_alias(session, entity_id=eid, user_id=USER, scope=SCOPE,
                               alias=" CAROL ", origin="llm", now=_now()) is False  # 同一键
        found = graph.find_entities_by_alias(session, user_id=USER, scope=SCOPE, alias="carol")
        assert [e.id for e in found] == [eid]
        assert graph.get_entity(session, other_id) is not None      # SAVEPOINT 没带倒它
        assert [a.alias for a in graph.list_aliases(session, entity_id=eid)] == ["Carol"]
        assert graph.delete_alias(session, entity_id=eid, alias="carol") == 1
        assert graph.find_entities_by_alias(session, user_id=USER, scope=SCOPE, alias="Carol") == []


def test_alias_deleted_entity_is_not_found(graph_db):
    with graph_db.db.session_scope() as session:
        eid = _mk_entity(session)
        graph.add_alias(session, entity_id=eid, user_id=USER, scope=SCOPE, alias="C",
                        origin="llm", now=_now())
        graph.set_entity_status(session, entity_id=eid, status=ENTITY_DELETED, now=_now())
        assert graph.find_entities_by_alias(session, user_id=USER, scope=SCOPE, alias="C") == []


# ---- 提及 + 消歧证据 ----


def test_mention_idempotent_and_resolution_survives_re_add(graph_db):
    """重复登记同一提及不覆盖已下的消歧结论（已 resolved 的行不会被改回 pending）。"""
    with graph_db.db.session_scope() as session:
        eid = _mk_entity(session)
        assert graph.add_mention(session, user_id=USER, scope=SCOPE, surface="她",
                                 memory_id="m1", turn_key="t1", status=MENTION_PENDING,
                                 generation=1, now=_now()) is True
        assert graph.add_mention(session, user_id=USER, scope=SCOPE, surface="她",
                                 memory_id="m1", turn_key="t1", status=MENTION_PENDING,
                                 generation=1, now=_now()) is False        # 幂等
        pending = graph.list_pending_mentions(session, user_id=USER, scope=SCOPE)
        assert [m.surface for m in pending] == ["她"]
        graph.resolve_mention(session, mention_id=pending[0].id, entity_id=eid,
                              status=MENTION_RESOLVED, evidence={"matched_alias": "Caroline"})
        # 重新登记同一提及:撞唯一键 → False,且既有消歧结论原样保留
        assert graph.add_mention(session, user_id=USER, scope=SCOPE, surface="她",
                                 memory_id="m1", turn_key="t1", status=MENTION_PENDING,
                                 generation=1, now=_now()) is False
        kept = graph.mentions_for_memory(session, "m1")
    assert kept[0].entity_id == eid and kept[0].status == MENTION_RESOLVED
    assert kept[0].evidence["matched_alias"] == "Caroline"


def test_unresolved_mention_is_representable(graph_db):
    """候选模糊时留 pending、entity_id=NULL —— 绝不强行绑定。"""
    with graph_db.db.session_scope() as session:
        graph.add_mention(session, user_id=USER, scope=SCOPE, surface="小李",
                          memory_id="m2", turn_key="", status=MENTION_PENDING,
                          generation=1, now=_now(), evidence={"candidates": 2})
        rows = graph.mentions_for_memory(session, "m2")
        assert len(rows) == 1 and rows[0].entity_id is None
        assert graph.list_pending_mentions(session, user_id=OTHER, scope=SCOPE) == []  # 隔离


# ---- 关系 ----


def _mk_pair(session, subject_name="Alice", object_name="Bob", user=USER, scope=SCOPE):
    return (_mk_entity(session, subject_name, user=user, scope=scope),
            _mk_entity(session, object_name, user=user, scope=scope))


def test_relation_dedupes_on_entity_object_and_text_object(graph_db):
    """去重键:宾语是实体 → 实体 id;宾语是文本 → 大小写 / 空白不敏感的名称键。"""
    with graph_db.db.session_scope() as session:
        subj, obj = _mk_pair(session)
        graph.create_relation(session, user_id=USER, scope=SCOPE, subject_entity_id=subj,
                              relation_type="knows", object_entity_id=obj, object_text="",
                              memory_id="m1", generation=1, now=_now())
        hit = graph.find_relations(session, user_id=USER, scope=SCOPE, subject_entity_id=subj,
                                   relation_type="knows", object_entity_id=obj, object_text="")
        assert len(hit) == 1
        graph.create_relation(session, user_id=USER, scope=SCOPE, subject_entity_id=subj,
                              relation_type="lives_in", object_entity_id=None,
                              object_text="Shenzhen", memory_id="m1", generation=1, now=_now())
        hit = graph.find_relations(session, user_id=USER, scope=SCOPE, subject_entity_id=subj,
                                   relation_type="lives_in", object_entity_id=None,
                                   object_text=" shenzhen ")
        assert len(hit) == 1 and hit[0].object_text == "Shenzhen"   # 原文保留


def test_relations_for_entity_covers_both_sides(graph_db):
    """遍历入口必须把实体作为主语**和**宾语的关系都算上（方向有意义,参与无方向）。"""
    with graph_db.db.session_scope() as session:
        subj, obj = _mk_pair(session)
        graph.create_relation(session, user_id=USER, scope=SCOPE, subject_entity_id=subj,
                              relation_type="knows", object_entity_id=obj, object_text="",
                              memory_id="m1", generation=1, now=_now())
        as_subject = graph.relations_for_entity(session, user_id=USER, scope=SCOPE,
                                                entity_id=subj)
        as_object = graph.relations_for_entity(session, user_id=USER, scope=SCOPE, entity_id=obj)
    assert [r.subject_entity_id for r in as_subject] == [subj]
    assert [r.object_entity_id for r in as_object] == [obj]


def test_update_relation_revision_and_event_time(graph_db):
    with graph_db.db.session_scope() as session:
        subj, obj = _mk_pair(session)
        rel = graph.create_relation(session, user_id=USER, scope=SCOPE, subject_entity_id=subj,
                                    relation_type="knows", object_entity_id=obj, object_text="",
                                    memory_id="m1", generation=1, now=_now())
        assert graph.update_relation(session, relation_id=rel.id, now=_now(),
                                     state="unknown") is False          # 没真变
        assert graph.update_relation(session, relation_id=rel.id, now=_now(),
                                     state="completed") is True
        assert graph.update_relation(session, relation_id=rel.id, now=_now(),
                                     event_time=None) is False         # 未给标志 → 不算变更
        moved = graph.update_relation(session, relation_id=rel.id, now=_now(),
                                      event_time=_now(), touch_event_time=True)
        after = graph.get_relation(session, rel.id)
    assert moved is True and after.state == "completed"
    assert after.event_time == _now() and after.revision == 3


# ---- 事件 + 参与者 ----


def test_event_dedupe_within_memory_and_soft_delete(graph_db):
    """同一次事件的重复提及按规范化描述合并（在调用方:先查重再决定新建）。"""
    with graph_db.db.session_scope() as session:
        ev = graph.create_event(session, user_id=USER, scope=SCOPE, kind="trip",
                                description="去了北京", memory_id="m1", generation=1,
                                now=_now(), time_expression="last summer",
                                time_precision="unknown", status="completed")
        assert ev.normalized == content_hash("去了北京")
        dup = graph.find_duplicate_event(session, memory_id="m1", description=" 去了北京 ")
        assert dup is not None and dup.id == ev.id
        assert graph.find_duplicate_event(session, memory_id="m2", description="去了北京") is None
        graph.update_event(session, event_id=ev.id, now=_now(), status="cancelled")
        after = graph.get_event(session, ev.id)
    assert after.status == "cancelled" and after.revision == 2


def test_participants_grouping_and_entity_link(graph_db):
    with graph_db.db.session_scope() as session:
        ev = graph.create_event(session, user_id=USER, scope=SCOPE, kind="meeting",
                                description="聚餐", memory_id="m1", generation=1, now=_now())
        assert graph.add_participant(session, event_id=ev.id, user_id=USER, scope=SCOPE,
                                     role="subject", name_text="Alice", now=_now()) is True
        assert graph.add_participant(session, event_id=ev.id, user_id=USER, scope=SCOPE,
                                     role="subject", name_text="alice", now=_now()) is False
        assert graph.add_participant(session, event_id=ev.id, user_id=USER, scope=SCOPE,
                                     role="companion", name_text="Bob", now=_now()) is True
        alice = _mk_entity(session, "Alice")
        assert graph.link_participant_entity(session, event_id=ev.id, role="subject",
                                             name_text="Alice", entity_id=alice) is True
        grouped = graph.participants_for_events(session, [ev.id])
    roles = {p.role: p for p in grouped[ev.id]}
    assert set(roles) == {"subject", "companion"}
    assert roles["subject"].entity_id == alice
    assert roles["companion"].entity_id is None                 # 未消歧:保留名称,不硬绑


# ---- 事实来源绑定 ----


def test_fact_link_multi_source_and_unlink(graph_db):
    """一条事实可有多个来源（不同轮次）;解绑一条记忆不动事实本身。"""
    with graph_db.db.session_scope() as session:
        subj, obj = _mk_pair(session)
        rel = graph.create_relation(session, user_id=USER, scope=SCOPE, subject_entity_id=subj,
                                    relation_type="knows", object_entity_id=obj, object_text="",
                                    memory_id="m1", generation=1, now=_now())
        assert graph.link_fact(session, fact_kind="relation", fact_id=rel.id, memory_id="m1",
                               memory_revision=1, user_id=USER, scope=SCOPE, turn_key="t1",
                               generation=1, now=_now(), quote="Alice knows Bob") is True
        assert graph.link_fact(session, fact_kind="relation", fact_id=rel.id, memory_id="m1",
                               memory_revision=1, user_id=USER, scope=SCOPE, turn_key="t1",
                               generation=1, now=_now()) is False           # 幂等
        assert graph.link_fact(session, fact_kind="relation", fact_id=rel.id, memory_id="m2",
                               memory_revision=2, user_id=USER, scope=SCOPE, turn_key="t2",
                               generation=1, now=_now()) is True            # 另一来源
        links = graph.links_for_fact(session, fact_kind="relation", fact_id=rel.id)
        assert {(l.memory_id, l.turn_key) for l in links} == {("m1", "t1"), ("m2", "t2")}
        # 删掉一条记忆:先解绑,再判孤儿 —— 这里只断言解绑,事实仍在(多来源的判孤在业务层)
        assert graph.unlink_memory(session, "m1") == 1
        assert graph.get_relation(session, rel.id) is not None
        rest = graph.links_for_fact(session, fact_kind="relation", fact_id=rel.id)
    assert [l.memory_id for l in rest] == ["m2"]


def test_links_for_facts_groups_and_validates_kind(graph_db):
    with graph_db.db.session_scope() as session:
        with pytest.raises(ValueError):
            graph.link_fact(session, fact_kind="guess", fact_id="x", memory_id="m", memory_revision=1,
                            user_id=USER, scope=SCOPE, turn_key="", generation=1, now=_now())
        graph.link_fact(session, fact_kind="event", fact_id="e1", memory_id="m1", memory_revision=1,
                        user_id=USER, scope=SCOPE, turn_key="t", generation=1, now=_now())
        graph.link_fact(session, fact_kind="event", fact_id="e1", memory_id="m1", memory_revision=1,
                        user_id=USER, scope=SCOPE, turn_key="u", generation=1, now=_now())
        graph.link_fact(session, fact_kind="event", fact_id="e2", memory_id="m1", memory_revision=1,
                        user_id=USER, scope=SCOPE, turn_key="t", generation=1, now=_now())
        grouped = graph.links_for_facts(session, fact_kind="event", fact_ids=["e1", "e2", "miss"])
    assert [l.turn_key for l in grouped["e1"]] == ["t", "u"]
    assert len(grouped["e2"]) == 1 and "miss" not in grouped


# ---- 删除 / 清除 / 计数 ----


def test_delete_event_cascades_participants(graph_db):
    with graph_db.db.session_scope() as session:
        ev = graph.create_event(session, user_id=USER, scope=SCOPE, kind="x",
                                description="d", memory_id="m", generation=1, now=_now())
        graph.add_participant(session, event_id=ev.id, user_id=USER, scope=SCOPE, role="subject",
                              name_text="A", now=_now())
        assert graph.delete_event(session, event_id=ev.id) == 1
        assert graph.get_event(session, ev.id) is None
        assert graph.participants_for_events(session, [ev.id]) == {}


def test_delete_graph_for_user_is_exact_and_isolated(graph_db):
    """彻底清除只动本用户本作用域:别的用户、别的作用域一行不少,返回各表行数。"""
    with graph_db.db.session_scope() as session:
        # 目标用户:全七张表都放行
        subj, obj = _mk_pair(session)
        rel = graph.create_relation(session, user_id=USER, scope=SCOPE, subject_entity_id=subj,
                                    relation_type="knows", object_entity_id=obj, object_text="",
                                    memory_id="m1", generation=1, now=_now())
        graph.link_fact(session, fact_kind="relation", fact_id=rel.id, memory_id="m1",
                        memory_revision=1, user_id=USER, scope=SCOPE, turn_key="t",
                        generation=1, now=_now())
        graph.add_mention(session, user_id=USER, scope=SCOPE, surface="Alice", entity_id=subj,
                          memory_id="m1", turn_key="t", status=MENTION_RESOLVED,
                          generation=1, now=_now())
        graph.add_alias(session, entity_id=subj, user_id=USER, scope=SCOPE, alias="Al",
                        origin="llm", now=_now())
        ev = graph.create_event(session, user_id=USER, scope=SCOPE, kind="x", description="d",
                                memory_id="m1", generation=1, now=_now())
        graph.add_participant(session, event_id=ev.id, user_id=USER, scope=SCOPE, role="subject",
                              name_text="Alice", now=_now())
        # 旁边的人:一个别的用户 + 一个别的作用域（都必须活下来）
        _mk_entity(session, "小明", user=OTHER)
        _mk_entity(session, "小明", scope="eval:keep")
        counts = graph.delete_graph_for_user(session, USER, scope=SCOPE)
        assert counts == {"participants": 1, "aliases": 1, "mentions": 1, "fact_links": 1,
                          "relations": 1, "events": 1, "entities": 2}
        assert graph.graph_counts(session, user_id=USER, scope=SCOPE) == {
            k: 0 for k in counts}
        assert graph.graph_counts(session, user_id=OTHER, scope=SCOPE)["entities"] == 1
        assert graph.graph_counts(session, user_id=USER, scope="eval:keep")["entities"] == 1


def test_pages_are_keyset_stable(graph_db):
    """整页游标（重建 / 对账用）:不重不漏,走到头自然停。"""
    with graph_db.db.session_scope() as session:
        ids = sorted(_mk_entity(session, f"e{i}") for i in range(5))
        seen, cursor = [], ""
        while True:
            page = graph.entities_page(session, scope=SCOPE, after_id=cursor, limit=2)
            if not page:
                break
            seen.extend(e.id for e in page)
            cursor = page[-1].id
        assert seen == ids
        assert graph.count_entities(session, user_id=USER, scope=SCOPE) == 5


def test_graph_counts_scope_only_and_with_user(graph_db):
    with graph_db.db.session_scope() as session:
        _mk_entity(session, "A")
        _mk_entity(session, "A", user=OTHER)
        assert graph.graph_counts(session, scope=SCOPE)["entities"] == 2
        assert graph.graph_counts(session, user_id=USER, scope=SCOPE)["entities"] == 1
        assert graph.graph_counts(session, user_id=USER, scope="eval:none")["entities"] == 0
