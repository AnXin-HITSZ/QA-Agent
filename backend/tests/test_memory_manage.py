"""「我的记忆」的自管理(§12):列表 / 检索 / 手添 / 编辑 / 删除 / 彻底清除。

覆盖的是**用户直接操作记忆的这条路**,与「会话提取」(test_memory_apply)分开:
- 归属:所有按 id 的操作都只认本人的行,别人的一律当不存在(越权不报错、不泄露存在性);
- 列表:默认不跑检索(列表不需要相关性排序,更不该为此付费向量化);带 query 才走检索,
  且检索结果仍要回 MySQL 验归属 / 状态(召回层给什么都不信);
- 编辑 / 删除:带版本 CAS + 审计 actor=user,向量清不干净不阻塞事实层的删除;
- 彻底清除:连同任务与对话正文一起删、审计正文脱敏、代次 +1 让清除之前的任务作废。

未覆盖:接口层(HTTP 状态码 / 认证)见 test_memory_api;真 MySQL 的并发行为见 test_memory_mysql。
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.memory import repo, service, vector
from app.memory.errors import (
    MemoryConflict, MemoryNotFound, MemoryStaleGeneration,
)
from app.memory.extract import ExtractedFact
from app.memory.models import (
    ACTOR_USER, EVENT_ADD, EVENT_DELETE, EVENT_UPDATE, JOB_KIND_EXTRACT, ORIGIN_USER,
    STATUS_ACTIVE, STATUS_DELETED,
)
from tests import memorykit

U1 = "00000000-0000-4000-8000-000000000001"
U2 = "00000000-0000-4000-8000-000000000002"

TEXT_A = "用户偏好用中文写实验记录"
TEXT_B = "用户喜欢喝咖啡"
TEXT_C = "用户每周五整理实验数据"
NOMATCH = "zzz"                      # 与所有正文没有共同词项 → BM25 一路必然没有命中

MANAGE_PARAMS = {
    "memory_vector_k": 3, "memory_bm25_k": 3, "memory_rerank_k": 3, "memory_top_k": 3,
    "memory_rrf_k": 60, "memory_context_chars": 2000, "memory_bm25_corpus_limit": 1000,
}


@pytest.fixture
def env(memory_db, cache_env, monkeypatch):
    """离线记忆环境(真表 + 内存 Qdrant + 假 Embeddings + 脚本化召回,见 memorykit)。"""
    s = memory_db.settings
    kit = memorykit.install(monkeypatch, settings=s, dim=s.embeddings_dim,
                            params=MANAGE_PARAMS)
    kit.db = memory_db.db
    kit.cache = cache_env
    return kit


def _seed(kit, *texts: str, user_id: str = U1) -> list[str]:
    return memorykit.seed(kit, kit.db, *texts, user_id=user_id)


def _ids(result: dict) -> list[str]:
    return [i.id for i in result["items"]]


def _texts(kit, *, user_id: str = U1) -> list[str]:
    """库里当前**有效**的正文(直查 repo,绕开接口,便于断言真实状态)。"""
    with kit.db.session_scope() as session:
        return [i.text for i in repo.list_items(session, user_id, status=STATUS_ACTIVE)]


# ---- 列表与检索 ----


def test_list_pages_and_only_returns_my_items(env):
    a, b, c = _seed(env, TEXT_A, TEXT_B, TEXT_C)
    _seed(env, "别人的事实", user_id=U2)

    first = service.list_items(user_id=U1, limit=2, offset=0)
    assert len(first["items"]) == 2
    assert first["total"] == 3                      # 总数是自己那三条
    second = service.list_items(user_id=U1, limit=2, offset=2)
    assert _ids(first) + _ids(second) != []
    assert set(_ids(first)) | set(_ids(second)) == {a, b, c}
    assert not (set(_ids(first)) & set(_ids(second)))   # 两页不重叠
    assert all(i.user_id == U1 for i in first["items"])


def test_list_clamps_paging_bounds(env):
    _seed(env, TEXT_A, TEXT_B)
    assert len(service.list_items(user_id=U1, limit=0)["items"]) == 1      # 下限 1,不是 0
    assert len(service.list_items(user_id=U1, limit=99999)["items"]) == 2  # 上限 200,不报错
    assert service.list_items(user_id=U1, offset=-5)["items"]              # 负偏移按 0 处理


def test_plain_list_never_calls_the_retrieval_path(env):
    """不带 query 的列表**不该**跑向量化 / 召回:那是付费调用,列表也不需要相关性排序。"""
    _seed(env, TEXT_A, TEXT_B)
    env.embeddings.texts.clear()
    env.dense.calls.clear()

    result = service.list_items(user_id=U1)

    assert len(result["items"]) == 2
    assert env.dense.calls == []
    assert env.embeddings.texts == []
    assert result["degraded"] == [] and result["error"] == ""


def test_search_list_uses_retrieval_and_validates_in_mysql(env):
    """带 query 的列表走检索,且**召回层给什么都不信**:别人的行与已删的行都进不来。"""
    a, b, _ = _seed(env, TEXT_A, TEXT_B, TEXT_C)
    _seed(env, "别人的事实", user_id=U2)
    with env.db.session_scope() as session:
        foreign = repo.list_items(session, U2, limit=1)[0].id
    service.delete_item(user_id=U1, memory_id=b)     # 已删的行:Qdrant 里可能还有残留点

    env.dense.ids = [foreign, b, a]                  # 替身「返回」了别人的 id 和一个旧 id

    result = service.list_items(user_id=U1, query=NOMATCH)

    assert _ids(result) == [a]
    assert env.dense.calls[-1]["user_id"] == U1      # 召回本身就是按用户查的
    assert result["counts"]["filtered_foreign"] == 1
    assert result["counts"]["filtered_missing"] == 1


def test_search_list_reports_degradation_without_failing(env):
    """向量一路挂了:如实记降级,不抛错、也不假装「你没有相关记忆」。"""
    _seed(env, TEXT_A, TEXT_B)
    env.dense.fail = RuntimeError("qdrant 不可达")

    result = service.list_items(user_id=U1, query=NOMATCH)

    assert result["items"] == []
    assert result["error"] == ""                     # 关键词一路还在,不算整体不可用
    assert any("向量" in note for note in result["degraded"])


# ---- 手添 ----


def test_add_writes_user_origin_audit_and_index(env):
    item = service.add_item(user_id=U1, text="  我的电话是10086  ")

    assert item.text == "我的电话是10086"              # 去首尾空白
    assert item.origin == ORIGIN_USER
    assert item.active and item.revision == 1
    assert item.indexed_at is not None                # 写完就补索引,不必等后台重放
    assert vector.count(U1) == 1

    audit = service.history(user_id=U1)
    assert [(h.event, h.actor, h.new_text) for h in audit] == [(EVENT_ADD, ACTOR_USER, item.text)]


def test_add_rejects_blank_and_duplicate(env):
    service.add_item(user_id=U1, text="爱吃辣")
    with pytest.raises(MemoryConflict):
        service.add_item(user_id=U1, text="   ")
    with pytest.raises(MemoryConflict):
        service.add_item(user_id=U1, text=" 爱吃辣 ")    # 只差空白 = 同一条事实
    assert _texts(env) == ["爱吃辣"]                    # 只落了一条


def test_add_clamps_to_configured_limit(env, monkeypatch):
    monkeypatch.setattr(env.settings, "memory_max_text_chars", 10)
    item = service.add_item(user_id=U1, text="很长的一条事实" * 10)
    assert item.text == ("很长的一条事实" * 10)[:10]


# ---- 编辑 ----


def test_edit_rewrites_bumps_revision_reindexes_and_audits(env):
    memory_id = _seed(env, TEXT_A)[0]
    original = service.list_items(user_id=U1)["items"][0]
    assert original.revision == 1

    item = service.edit_item(user_id=U1, memory_id=memory_id, text="用户偏好用英文写实验记录")

    assert item.id == memory_id
    assert item.revision == 2
    assert item.origin == "llm"                        # 来源记「最初怎么来的」,改写不改它
    assert item.indexed_at is not None
    assert vector.count(U1) == 1                       # 还是那一个点(按 id 覆盖,没写重)
    events = [(h.event, h.actor, h.old_text, h.new_text) for h in service.history(user_id=U1)]
    assert events[0] == (EVENT_UPDATE, ACTOR_USER, TEXT_A, "用户偏好用英文写实验记录")
    assert _texts(env) == ["用户偏好用英文写实验记录"]


def test_edit_of_foreign_or_missing_id_is_not_found(env):
    _seed(env, "别人的事实", user_id=U2)
    with env.db.session_scope() as session:
        foreign = repo.list_items(session, U2, limit=1)[0].id

    with pytest.raises(MemoryNotFound):
        service.edit_item(user_id=U1, memory_id=foreign, text="改别人的")
    with pytest.raises(MemoryNotFound):
        service.edit_item(user_id=U1, memory_id="00000000-0000-4000-8000-00000000dead",
                          text="改个不存在的")
    assert _texts(env, user_id=U2) == ["别人的事实"]    # 一个字都没动


def test_edit_rejects_blank_text(env):
    memory_id = _seed(env, TEXT_A)[0]
    with pytest.raises(MemoryConflict):
        service.edit_item(user_id=U1, memory_id=memory_id, text="   ")
    assert _texts(env) == [TEXT_A]


def test_edit_loses_to_a_concurrent_change(env, monkeypatch):
    """用户读到的版本被别人改过之后,这次编辑不覆盖 —— 宁可让他重看一遍再改。"""
    memory_id = _seed(env, TEXT_A)[0]
    real_get = repo.get_item

    def racing_get(session, user_id, mid):
        item = real_get(session, user_id, mid)
        if item is not None:
            # 在**另一个已提交的事务**里改这一行(真实的并发方),再返回改动前的快照
            with env.db.session_scope() as other:
                repo.update_item(other, user_id=user_id, memory_id=mid, new_text="并发改成的正文",
                                 actor="llm", now=item.updated_at, reason="并发")
        return item

    monkeypatch.setattr(repo, "get_item", racing_get)
    with pytest.raises(MemoryConflict):
        service.edit_item(user_id=U1, memory_id=memory_id, text="我这次的编辑")

    monkeypatch.setattr(repo, "get_item", real_get)
    assert _texts(env) == ["并发改成的正文"]


# ---- 删除单条 ----


def test_delete_soft_deletes_the_row_and_clears_the_vector(env):
    a, b = _seed(env, TEXT_A, TEXT_B)

    result = service.delete_item(user_id=U1, memory_id=a)
    item = result["item"]

    assert item.status == STATUS_DELETED and item.deleted_at is not None
    assert result["cleanup"] == "done" and result["vector_cleaned"] is True
    assert result["op_id"]                              # 台账登记了(立刻清成功即收掉)
    with env.db.session_scope() as session:
        assert repo.op_counts(session, user_id=U1)["pending"] == 0
    assert vector.count(U1) == 1                       # 只删了 a 的点,b 还在
    assert _texts(env) == [TEXT_B]
    with env.db.session_scope() as session:            # 行与审计都还在(可追溯,不是物理删)
        rows = repo.list_items(session, U1, status=STATUS_DELETED)
        assert [r.id for r in rows] == [a]
    events = [h.event for h in service.history(user_id=U1, memory_id=a)]   # 新的在前
    assert events == [EVENT_DELETE, EVENT_ADD]


def test_delete_twice_is_not_found(env):
    memory_id = _seed(env, TEXT_A)[0]
    service.delete_item(user_id=U1, memory_id=memory_id)
    with pytest.raises(MemoryNotFound):
        service.delete_item(user_id=U1, memory_id=memory_id)


def test_delete_survives_a_vector_cleanup_failure(env, monkeypatch):
    """事实层删掉才算数:向量清不掉只是残留(检索时会被 MySQL 校验剔除),不阻塞删除。

    但**要说出来、并留下可重试的台账**(cleanup=pending);界面据此显示「正在清理」,
    而不是笼统的「删除完成」。
    """
    memory_id = _seed(env, TEXT_A)[0]

    def boom(_ids, **_kwargs):
        raise RuntimeError("qdrant 不可达")

    monkeypatch.setattr(vector, "delete_ids", boom)

    result = service.delete_item(user_id=U1, memory_id=memory_id)

    assert result["item"].status == STATUS_DELETED
    assert result["cleanup"] == "pending" and result["vector_cleaned"] is False
    assert _texts(env) == []
    with env.db.session_scope() as session:              # 台账留着,worker 会按退避重试
        counts = repo.op_counts(session, user_id=U1)
    assert counts["pending"] == 1 and counts["failed"] == 0


# ---- 审计历史 ----


def test_history_is_user_scoped_and_paginated(env):
    a, b = _seed(env, TEXT_A, TEXT_B)
    _seed(env, "别人的事实", user_id=U2)

    assert len(service.history(user_id=U1)) == 2
    assert len(service.history(user_id=U1, limit=1)) == 1
    assert service.history(user_id=U1, offset=2) == []
    assert [h.new_text for h in service.history(user_id=U1, memory_id=a)] == [TEXT_A]
    assert all(h.user_id == U1 for h in service.history(user_id=U1))    # 别人的一行不露
    assert len(service.history(user_id=U1, memory_id=b)) == 1


# ---- 彻底清除 ----


def _enqueue(kit, *, user_id: str = U1, key: str = "k1") -> str | None:
    return service.enqueue_extraction(
        user_id=user_id, thread_id=None, dedupe_key=key,
        messages=[{"role": "user", "content": "我住在深圳"},
                  {"role": "assistant", "content": "好的"}],
    )


def test_clear_wipes_facts_jobs_and_redacts_audit(env):
    _seed(env, TEXT_A, TEXT_B)
    _enqueue(env, key="round-1")

    result = service.clear_user(user_id=U1)

    assert result["items"] == 2
    assert result["jobs"] == 1
    assert result["history"] >= 2                      # 审计行保留,但正文脱敏
    assert result["generation"] == 1
    assert result["vectors"] is True and result["cleanup"] == "done"
    assert result["degraded"] == []
    assert _texts(env) == []
    assert vector.count(U1) == 0
    with env.db.session_scope() as session:
        audit = repo.list_history(session, U1)         # 只留下「发生过一次删除」这条线索
        assert audit and all(h.old_text is None and h.new_text is None for h in audit)
        assert repo.job_counts(session, U1)["pending"] == 0


def test_clear_does_not_touch_other_users(env):
    _seed(env, TEXT_A)
    _seed(env, "别人的事实", user_id=U2)

    service.clear_user(user_id=U1)

    assert _texts(env, user_id=U2) == ["别人的事实"]
    assert vector.count(U2) == 1


def test_clear_reports_degraded_when_vector_cleanup_fails(env, monkeypatch):
    _seed(env, TEXT_A)

    def boom(_user_id, **_kwargs):
        raise RuntimeError("qdrant 不可达")

    monkeypatch.setattr(vector, "delete_user", boom)

    result = service.clear_user(user_id=U1)

    assert result["items"] == 1 and result["vectors"] is False
    assert result["cleanup"] == "pending"              # 界面据此显示「正在清理」,不报「已完成」
    assert result["degraded"] and "向量" in result["degraded"][0]
    assert _texts(env) == []                           # 事实照样删干净(不粉饰,也不半途而废)
    with env.db.session_scope() as session:            # 台账留着,worker 会按退避重试
        assert repo.op_counts(session, user_id=U1)["pending"] == 1


def test_a_write_from_before_the_clear_is_voided_not_silently_skipped(env):
    """清除之后,清除之前的任务带着旧代次来写:整批作废并**明确报错**。

    关键在「明确」:这条路径如果被当成「已有相同事实,跳过」,任务会显示成功、
    用户以为记下了 —— 而实际一条都没写。
    """
    _enqueue(env, key="round-1")
    service.clear_user(user_id=U1)
    env.embeddings.texts.clear()

    facts = [ExtractedFact(text="用户住在深圳", kind="profile")]
    with pytest.raises(MemoryStaleGeneration):
        service.write_facts(user_id=U1, facts=facts, generation=0)

    assert _texts(env) == []                           # 一个字都没写回来
    env.settings.memory_maintenance_enabled = False    # 维护关闭的回退路径同样要作废
    try:
        with pytest.raises(MemoryStaleGeneration):
            service.apply_facts(user_id=U1, facts=facts, generation=0)
    finally:
        env.settings.memory_maintenance_enabled = True
    assert _texts(env) == []
    assert env.embeddings.texts == []                  # 作废发生在落库前,不产生向量化调用


def test_writes_after_the_clear_use_the_new_generation(env):
    _seed(env, TEXT_A)
    service.clear_user(user_id=U1)

    result = service.write_facts(user_id=U1, facts=[ExtractedFact(text="用户住在深圳",
                                                                 kind="profile")],
                                 generation=1)

    assert [o.status for o in result.per_fact] == ["added"]
    assert _texts(env) == ["用户住在深圳"]              # 清除之后的正常写入不受影响


def test_clear_is_idempotent_and_keeps_bumping(env):
    _seed(env, TEXT_A)
    first = service.clear_user(user_id=U1)
    second = service.clear_user(user_id=U1)
    assert (first["generation"], second["generation"]) == (1, 2)
    assert second["items"] == 0 and second["jobs"] == 0
    assert get_settings().memory_enabled                # 清除 ≠ 关掉记忆功能
