"""维护路径落库(§6):apply_facts 的 ADD / UPDATE / DELETE / NONE 与如实汇报。

环境与检索测试同一套(真表 + 内存 Qdrant + 假 Embeddings);这里额外把**候选召回也走真实
BM25**——不脚本化稠密名次,靠正文里共同的中文词让候选自然出现,顺带验证「决策拿到的是
真候选」。模型输出仍要脚本化(真实模型判得准不准只能在有凭证的环境里看)。
"""

from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.memory import db, repo, search, service, vector
from app.memory.extract import ExtractedFact
from app.memory.models import ACTOR_USER, STATUS_ACTIVE, STATUS_DELETED
from tests import memorykit
from tests.memorykit import FakeLLM

U1 = "00000000-0000-4000-8000-000000000001"
U2 = "00000000-0000-4000-8000-000000000002"

OLD = "用户偏好用 Word 写实验记录"
NEW = "用户改用 Markdown 写实验记录"

MAINTAIN_PARAMS = {
    "memory_vector_k": 3, "memory_bm25_k": 3, "memory_rerank_k": 3, "memory_top_k": 3,
    "memory_rrf_k": 60, "memory_bm25_corpus_limit": 1000, "memory_context_chars": 2000,
    "memory_rerank_max_docs": 50, "memory_rerank_max_chars": 8000,
    "memory_maintenance_vector_k": 5, "memory_maintenance_bm25_k": 5,
    "memory_maintenance_rerank_k": 5, "memory_maintenance_top_k": 4,
    "memory_maintenance_enabled": True, "memory_maintenance_retries": 1,
    "memory_maintenance_max_actions": 5,
}


@pytest.fixture
def apply_env(memory_db, cache_env, monkeypatch):
    env = memorykit.install(monkeypatch, settings=memory_db.settings,
                            dim=memory_db.settings.embeddings_dim, params=MAINTAIN_PARAMS)
    env.db = memory_db.db
    env.cache = cache_env
    return env


def _seed(env, *texts: str, user_id: str = U1) -> list[str]:
    return memorykit.seed(env, env.db, *texts, user_id=user_id)


def _fact(text: str) -> ExtractedFact:
    return ExtractedFact(text=text, kind="preference")


def _events(*events: dict, reason: str = "测试") -> str:
    return json.dumps({"events": list(events), "reason": reason}, ensure_ascii=False)


def _apply(env, *facts: str, llm=None, user_id: str = U1) -> service.WriteResult:
    return service.apply_facts(user_id=user_id, facts=[_fact(t) for t in facts], llm=llm)


def _active(env, user_id: str = U1) -> list[str]:
    with db.session_scope() as session:
        return [item.text for item in repo.list_items(session, user_id)]


def _row(env, memory_id: str, user_id: str = U1):
    with db.session_scope() as session:
        return repo.get_item(session, user_id, memory_id)


def _history(env, user_id: str = U1) -> list[tuple[str, str]]:
    with db.session_scope() as session:
        return [(h.event, h.memory_id) for h in repo.list_history(session, user_id)]


def _never_called(**kwargs):
    """一个「被调用就炸」的模型:用来钉死「这条路径不该花模型调用」。"""
    return FakeLLM(error=AssertionError("这条路径不该调用模型"))


# ---- 纯新增(没有可比的旧记忆) ----


def test_no_candidate_means_no_model_call(apply_env):
    result = _apply(apply_env, "用户偏好用 Markdown 写实验记录", llm=_never_called())

    assert result.per_fact[0].status == service.FACT_ADDED
    assert _active(apply_env) == ["用户偏好用 Markdown 写实验记录"]
    assert result.index.indexed == 1 and result.index.deferred == 0
    # 一条记忆都没写过时集合还不存在,稀疏通道按「用不了」处理并如实降级(不假装查过)
    assert all("降级" in note or "不可用" in note for note in result.degraded), result.degraded
    assert not any("未查重" in note for note in result.degraded)   # 候选为空不是「拿不到候选」


def test_maintenance_off_falls_back_to_plain_add_and_says_so(apply_env, monkeypatch):
    monkeypatch.setattr(apply_env.settings, "memory_maintenance_enabled", False)
    _seed(apply_env, OLD)

    result = _apply(apply_env, NEW, llm=_never_called())

    assert result.per_fact[0].status == service.FACT_ADDED
    assert sorted(_active(apply_env)) == sorted([OLD, NEW])     # 没改也没删旧记忆
    assert service.MAINTENANCE_OFF_NOTE in result.degraded


def test_blank_fact_is_reported_as_failed_and_kept_in_order(apply_env):
    """正文为空的兜底:ExtractedFact 自己就拦(见下),service 这层再拦一次并如实汇报。"""
    blank = ExtractedFact.model_construct(text="   ", kind="preference")
    facts = [blank, _fact("用户偏好用 Markdown 写实验记录")]

    result = service.apply_facts(user_id=U1, facts=facts, llm=_never_called())

    assert [o.status for o in result.per_fact] == [service.FACT_FAILED, service.FACT_ADDED]
    assert result.outcome.dropped == ["正文为空,已丢弃"]
    assert len(result.per_fact) == 2                            # 与输入同序、一条不落


# ---- 改写 ----


def test_update_rewrites_the_row_and_records_history(apply_env):
    (mid,) = _seed(apply_env, OLD)
    llm = FakeLLM(_events({"event": "UPDATE", "id": mid, "text": NEW}, reason="偏好变了"))

    result = _apply(apply_env, "用户现在改用 Markdown 写实验记录", llm=llm)

    assert len(llm.calls) == 1
    assert _active(apply_env) == [NEW]
    assert [o.status for o in result.per_fact] == [service.FACT_UPDATED]
    assert result.per_fact[0].memory_ids == [mid]
    assert [item.id for item in result.outcome.updated] == [mid]
    assert result.outcome.deleted == [] and result.outcome.added == []
    assert (("UPDATE", mid) in _history(apply_env))
    row = _row(apply_env, mid)
    assert row.revision == 2 and row.status == STATUS_ACTIVE
    assert result.index.indexed == 1                            # 改写后重新索引


def test_none_means_nothing_changes(apply_env):
    (mid,) = _seed(apply_env, OLD)
    llm = FakeLLM(_events({"event": "NONE"}, reason="已经记过"))

    result = _apply(apply_env, "用户用 Word 写实验记录", llm=llm)

    assert [o.status for o in result.per_fact] == [service.FACT_UNCHANGED]
    assert result.per_fact[0].reason == "已经记过"
    assert _row(apply_env, mid).revision == 1
    assert result.outcome.skipped == 1 and result.index.requested == 0


def test_conflicting_fact_deletes_the_old_and_adds_the_new(apply_env):
    (mid,) = _seed(apply_env, OLD)
    llm = FakeLLM(_events({"event": "DELETE", "id": mid}, {"event": "ADD", "text": NEW}))

    result = _apply(apply_env, "用户不再用 Word 写实验记录了", llm=llm)

    assert _active(apply_env) == [NEW]
    assert _row(apply_env, mid).status == STATUS_DELETED
    assert [o.status for o in result.per_fact] == [service.FACT_DELETED]
    assert set(result.per_fact[0].memory_ids) == {mid, result.outcome.added[0].id}
    assert [item.id for item in result.outcome.deleted] == [mid]
    assert {("DELETE", mid)} <= set(_history(apply_env))
    assert result.index.indexed == 1 and result.index.deferred == 0


def test_delete_and_add_survive_or_fail_together(apply_env, monkeypatch):
    """同一个事务:新增炸了,删除也必须回滚 —— 否则旧记忆凭空消失、新的又没写进去。"""
    (mid,) = _seed(apply_env, OLD)
    llm = FakeLLM(_events({"event": "DELETE", "id": mid}, {"event": "ADD", "text": NEW}))

    def boom(*args, **kwargs):
        raise RuntimeError("模拟落库中途失败")

    monkeypatch.setattr(repo, "create_item", boom)

    with pytest.raises(RuntimeError):
        _apply(apply_env, "用户不再用 Word 写实验记录了", llm=llm)

    assert _active(apply_env) == [OLD]                # 删除没有留下痕迹
    assert _row(apply_env, mid).status == STATUS_ACTIVE


def test_the_user_edit_wins_when_the_decision_is_stale(apply_env):
    """决策是基于某个版本做出的:期间用户自己改过,整个决策都不执行(不回退成部分执行)。"""
    (mid,) = _seed(apply_env, OLD)
    edited = "用户偏好用 Word 写实验记录(我自己改的)"

    class EditDuringDecide(FakeLLM):
        def invoke(self, messages, **kwargs):
            with db.session_scope() as session:
                repo.update_item(session, user_id=U1, memory_id=mid, new_text=edited,
                                 actor=ACTOR_USER, now=db.utc_naive())
            return super().invoke(messages, **kwargs)

    llm = EditDuringDecide(_events({"event": "UPDATE", "id": mid, "text": NEW}))
    result = _apply(apply_env, "用户现在改用 Markdown 写实验记录", llm=llm)

    assert _active(apply_env) == [edited]              # 用户的编辑保住了
    assert [o.status for o in result.per_fact] == [service.FACT_FAILED]
    assert "revision" in result.per_fact[0].reason     # 说清是「版本对不上」而不是别的
    assert any("未能整体生效" in note for note in result.degraded)
    assert result.pending == [0]                       # 交回任务重试(重新召回 + 重新决策),不静默丢


def test_a_stale_decision_is_rolled_back_whole(apply_env):
    """同一条决策里的 DELETE + ADD:只要有一个事件对不上版本,**另一个也不许执行**。

    「能做的先做掉」在这里等于永久丢内容:旧记忆删了、新说法没写进去。
    """
    (mid,) = _seed(apply_env, OLD)
    edited = "用户偏好用 Word 写实验记录(我自己改的)"

    class EditDuringDecide(FakeLLM):
        def invoke(self, messages, **kwargs):
            with db.session_scope() as session:
                repo.update_item(session, user_id=U1, memory_id=mid, new_text=edited,
                                 actor=ACTOR_USER, now=db.utc_naive())
            return super().invoke(messages, **kwargs)

    llm = EditDuringDecide(_events({"event": "DELETE", "id": mid}, {"event": "ADD", "text": NEW}))
    result = _apply(apply_env, "用户不再用 Word 写实验记录了", llm=llm)

    assert _active(apply_env) == [edited]              # 没删、也没新增
    assert [o.status for o in result.per_fact] == [service.FACT_FAILED]
    assert result.outcome.added == [] and result.outcome.deleted == []


def test_a_model_that_repeats_the_same_text_is_treated_as_unchanged(apply_env):
    (mid,) = _seed(apply_env, OLD)
    llm = FakeLLM(_events({"event": "UPDATE", "id": mid, "text": f" {OLD} "}))

    result = _apply(apply_env, "用户用 Word 写实验记录", llm=llm)

    row = _row(apply_env, mid)
    assert row.revision == 1 and row.meta_version == 0     # 没有无意义地 +1,也不刷 payload
    assert [o.status for o in result.per_fact] == [service.FACT_UNCHANGED]
    assert "等价" in result.per_fact[0].reason             # 如实说是「没有实质变化」
    assert result.index.requested == 0                     # 连索引都不用碰


def test_a_duplicate_add_is_skipped_not_an_error(apply_env):
    _seed(apply_env, OLD)
    llm = FakeLLM(_events({"event": "ADD", "text": f" {OLD} "}))   # 归一后与已有记忆相同

    result = _apply(apply_env, "用户用 Word 写实验记录", llm=llm)

    assert _active(apply_env) == [OLD]                 # 没有写下第二条一模一样的事实
    assert [o.status for o in result.per_fact] == [service.FACT_UNCHANGED]
    assert "已存在" in result.per_fact[0].reason        # 模型要的事已经满足了,不是失败
    assert result.outcome.skipped == 1


# ---- 授权与降级 ----


def test_an_id_the_model_made_up_never_touches_another_users_memory(apply_env):
    """模型引用了**没展示给它**的 id(别人的记忆):整条决策不采纳,一个事件都不执行。

    别人的记忆当然毫发无损;同一份决策里那条「看起来合法」的删除也不做 —— 一次越权输出
    不能被当成部分授权(见 maintain.authorize)。
    """
    (mine,) = _seed(apply_env, OLD)
    (theirs,) = _seed(apply_env, "用户偏好用 Word 写实验记录", user_id=U2)
    llm = FakeLLM(_events({"event": "DELETE", "id": theirs}, {"event": "DELETE", "id": mine}))

    result = _apply(apply_env, "用户不再用 Word 写实验记录了", llm=llm)

    assert _row(apply_env, theirs, user_id=U2).status == STATUS_ACTIVE   # 别人的记忆毫发无损
    assert _row(apply_env, mine).status == STATUS_ACTIVE                 # 这条也没被执行
    assert [o.status for o in result.per_fact] == [service.FACT_FAILED]
    assert result.pending == [0] and result.rejected       # 交回重试,不静默当成做完了
    # 授权失败是模型自己能改对的一类错:带纠正提示重试过一次(MAINTAIN_PARAMS 里 retries=1)
    assert len(llm.calls) == 2


def test_unparsable_decision_fails_the_fact_instead_of_adding_it_anyway(apply_env):
    _seed(apply_env, OLD)
    llm = FakeLLM("我觉得不用改")

    result = _apply(apply_env, NEW, llm=llm)

    assert [o.status for o in result.per_fact] == [service.FACT_FAILED]
    assert result.per_fact[0].reason                      # 原因要写清,不能只说"没成功"
    assert _active(apply_env) == [OLD]                    # 也绝不偷偷按新增落库
    assert result.index.requested == 0


def test_recall_failure_degrades_to_plain_add_and_never_claims_to_have_deduped(
        apply_env, monkeypatch):
    _seed(apply_env, OLD)

    def boom(*args, **kwargs):
        raise RuntimeError("mysql 读不到正文")

    # 两路召回都得停掉才算「整体不可用」:稠密挂 + 稀疏通道不可用 + 降级 BM25 读不到语料
    apply_env.dense.fail = RuntimeError("qdrant 也不可用")
    memorykit.sparse_missing(monkeypatch)
    monkeypatch.setattr(repo, "active_texts", boom)

    result = _apply(apply_env, NEW, llm=_never_called())

    assert result.per_fact[0].status == service.FACT_ADDED
    assert NEW in _active(apply_env)                     # 事实不会因为检索挂了就丢掉
    assert any("未查重" in note for note in result.degraded)


def test_partial_recall_failure_is_reported_but_still_maintained(apply_env):
    (mid,) = _seed(apply_env, OLD)
    apply_env.dense.fail = RuntimeError("qdrant 不可用")      # 关键词那路还活着
    llm = FakeLLM(_events({"event": "UPDATE", "id": mid, "text": NEW}))

    result = _apply(apply_env, "用户现在改用 Markdown 写实验记录", llm=llm)

    assert _active(apply_env) == [NEW]                   # BM25 召回到了候选,维护照常
    assert result.degraded and any("向量" in note for note in result.degraded)


def test_vector_cleanup_failure_is_reported_not_hidden(apply_env, monkeypatch):
    (mid,) = _seed(apply_env, OLD)
    llm = FakeLLM(_events({"event": "DELETE", "id": mid}, {"event": "ADD", "text": NEW}))

    def boom(*args, **kwargs):
        raise RuntimeError("qdrant 写不进去")

    monkeypatch.setattr(vector, "delete_ids", boom)

    result = _apply(apply_env, "用户不再用 Word 写实验记录了", llm=llm)

    assert _active(apply_env) == [NEW]                   # 事实层面照常完成
    assert any("向量清理失败" in note for note in result.degraded)


def test_index_failure_leaves_the_rows_pending_for_replay(apply_env, monkeypatch):
    (mid,) = _seed(apply_env, OLD)
    llm = FakeLLM(_events({"event": "UPDATE", "id": mid, "text": NEW}))

    def boom(*args, **kwargs):
        raise RuntimeError("embedding 端点 500")

    monkeypatch.setattr(service, "get_embeddings", boom)

    result = _apply(apply_env, "用户现在改用 Markdown 写实验记录", llm=llm)

    assert _active(apply_env) == [NEW]                   # 事实写成了
    assert result.index.deferred == 1 and result.index.error
    assert _row(apply_env, mid).embedding_version == ""  # 留在待索引,由重放补齐


# ---- 参数来源 ----


def test_settings_carry_the_maintenance_defaults():
    s = get_settings()
    assert s.memory_maintenance_enabled is True
    assert s.memory_maintenance_retries >= 0
    assert s.memory_maintenance_max_actions >= 1
    assert s.memory_rrf_k > 0 and s.memory_context_chars > 0
