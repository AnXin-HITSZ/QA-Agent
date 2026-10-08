"""代码审查边界回归：真实表 + 内存 Qdrant，全部离线。"""
from datetime import timedelta
from dataclasses import replace

import pytest
from qdrant_client.models import PointIdsList

from app.memory import extract, repo, search, service, vector
from app.memory.errors import MemoryExtractionError
from app.memory.models import ACTOR_USER, OP_DELETE_VECTOR
from app.memory.worker import MemoryWorker
from tests.test_memory_service import U1, _fact, mem_env
from tests.test_memory_worker import worker_env, _enqueue, _install_llm


def _published_snapshot(env, *, payload_only: bool):
    """造一条「索引点已发布、事实行比它更新」的快照（触发发布后复核里的清理分支）。

    - `payload_only=False`：正文改过 → 快照进 **needs-vector** 路径（重新向量化）；
    - `payload_only=True`：正文没动、只改元数据 → 快照进 **只刷 payload** 路径。
      这条路径必须**重新读行**才能进得去：`write_facts` 返回的是建行时的对象，
      `embedding_version` 还是空串，`vector_is_current()` 恒为假 —— 换个字段凑不出来。
    """
    item = service.write_facts(user_id=U1, facts=[_fact("用户偏好中文")]).outcome.added[0]
    if payload_only:
        with env.db.session_scope() as session:          # 只动元数据:正文与索引点都不动
            env.repo.update_item(session, user_id=U1, memory_id=item.id, new_text=item.text,
                                 kind="task", actor=ACTOR_USER, now=env.db.utc_naive(),
                                 reason="补类型")
        with env.db.session_scope() as session:
            fresh = env.repo.get_item(session, U1, item.id)
        version = service.embedding_version()
        assert fresh.vector_is_current(version) and not fresh.index_synced(version)
        return fresh
    service.edit_item(user_id=U1, memory_id=item.id, text="用户偏好英文")
    return item                                          # 快照版本落后:走 needs-vector


def _advance_revision(monkeypatch, snapshot):
    """模拟「复核时这一行已被并发改写」:版本前进一格,旧点仍在集合里。"""
    real = repo.list_by_ids

    def newer(session, ids):
        assert real(session, ids)                        # 行确实还在,只是版本前进了
        return [replace(snapshot, revision=snapshot.revision + 1)]

    monkeypatch.setattr(repo, "list_by_ids", newer)


@pytest.mark.parametrize("payload_only", [False, True])
def test_cleanup_failure_does_not_fail_published_index(mem_env, monkeypatch, payload_only):
    snapshot = _published_snapshot(mem_env, payload_only=payload_only)
    if payload_only:
        _advance_revision(monkeypatch, snapshot)
    monkeypatch.setattr(vector, "delete_older_revisions",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline")))
    result = service._index_items([snapshot])
    assert result.deferred == 0
    assert not result.error
    if payload_only:
        # 证明这次真的走的是「只刷 payload」那条(而不是被当成 needs-vector 又向量化一遍)
        assert result.payload_only == 1 and result.indexed == 0
    with mem_env.db.session_scope() as session:
        assert any(op.kind == "delete_old_versions" and op.status == "pending"
                   for op in repo.list_ops(session, user_id=U1))


@pytest.mark.parametrize("payload_only", [False, True])
@pytest.mark.parametrize("failed_call", ["list_by_ids", "enqueue_op", "finish_pending_op"])
def test_reconcile_database_failure_does_not_fail_published_index(mem_env, monkeypatch, payload_only, failed_call):
    snapshot = _published_snapshot(mem_env, payload_only=payload_only)
    if payload_only and failed_call != "list_by_ids":
        _advance_revision(monkeypatch, snapshot)         # 否则走不到登记 / 收尾那两步
    monkeypatch.setattr(repo, failed_call,
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("database offline")))
    result = service._index_items([snapshot])
    assert result.deferred == 0
    assert not result.error
    if payload_only:
        assert result.payload_only == 1


def test_deleted_snapshot_cannot_republish_after_cleanup(mem_env):
    snapshot = service.write_facts(user_id=U1, facts=[_fact("用户偏好中文")]).outcome.added[0]
    assert service.delete_item(user_id=U1, memory_id=snapshot.id)["cleanup"] == "done"
    service._index_items([snapshot])
    assert vector.count(U1) == 0


def test_clear_during_upsert_cleans_late_point(mem_env, monkeypatch):
    snapshot = service.write_facts(user_id=U1, facts=[_fact("用户偏好中文")]).outcome.added[0]
    upsert = vector.upsert

    def clear_then_write(points):
        assert service.clear_user(user_id=U1)["cleanup"] == "done"
        return upsert(points)

    monkeypatch.setattr(vector, "upsert", clear_then_write)
    service._index_items([snapshot])
    assert vector.count(U1) == 0
    assert not service.list_items(user_id=U1)["items"]


def test_late_point_cleanup_failure_remains_recoverable(mem_env, monkeypatch):
    snapshot = service.write_facts(user_id=U1, facts=[_fact("用户偏好中文")]).outcome.added[0]
    service.clear_user(user_id=U1)
    delete = vector.delete_ids
    monkeypatch.setattr(vector, "delete_ids",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline")))
    service._index_items([snapshot])
    assert vector.count(U1) == 1
    with mem_env.db.session_scope() as session:
        assert repo.op_counts(session, user_id=U1)["pending"] == 1
    monkeypatch.setattr(vector, "delete_ids", delete)
    MemoryWorker()._replay_ops()
    assert vector.count(U1) == 0


def test_orphan_after_publish_crash_is_registered_and_replayed(mem_env):
    snapshot = service.write_facts(user_id=U1, facts=[_fact("用户偏好中文")]).outcome.added[0]
    service.clear_user(user_id=U1)
    point = vector.make_point(snapshot, mem_env.embeddings.embed_query(snapshot.text),
                              embedding_version=service.embedding_version())
    vector.upsert([point])  # 模拟发布后、后置复核前进程退出
    service.index_drift(user_id=U1)
    with mem_env.db.session_scope() as session:
        assert any(op.kind == OP_DELETE_VECTOR for op in repo.list_ops(session, user_id=U1))
    MemoryWorker()._replay_ops()
    assert vector.count(U1) == 0


def test_old_point_cannot_mask_missing_current_point(mem_env):
    first = service.write_facts(user_id=U1, facts=[_fact("用户偏好中文")]).outcome.added[0]
    second = service.write_facts(user_id=U1, facts=[_fact("用户使用Python")]).outcome.added[0]
    service.edit_item(user_id=U1, memory_id=first.id, text="用户偏好英文")
    vector.upsert([vector.make_point(first, mem_env.embeddings.embed_query(first.text),
                                     embedding_version=service.embedding_version())])
    mem_env.qdrant.delete(collection_name=vector.collection_name(),
                         points_selector=PointIdsList(points=[vector.point_id(second.id, second.revision)]))
    drift = service.index_drift(user_id=U1)
    assert drift["points"] == drift["items"] == 2
    assert drift["drift"] and drift["mismatched"] == 1
    service.ensure_indexed(user_id=U1, repair=True)
    assert not service.index_drift(user_id=U1)["drift"]


def test_final_evidence_keeps_scored_revision(mem_env):
    snapshot = service.write_facts(user_id=U1, facts=[_fact("用户偏好中文")]).outcome.added[0]
    ranked = [(snapshot.id, 1.0, 1.0, 1, None, "rerank")]
    service.edit_item(user_id=U1, memory_id=snapshot.id, text="用户偏好英文")
    hits, note = search._finalize(ranked, limit=1, budget=2000, snapshots={snapshot.id: snapshot})
    assert not hits and "变化" in note


def test_mutation_during_rerank_does_not_return_new_text_with_old_score(mem_env, monkeypatch):
    snapshot = service.write_facts(user_id=U1, facts=[_fact("用户偏好中文")]).outcome.added[0]
    monkeypatch.setattr(search.rerank_client, "configured", lambda: True)

    def rerank(*args, **kwargs):
        service.edit_item(user_id=U1, memory_id=snapshot.id, text="用户正在办理报销")
        return search.rerank_client.RerankOutcome(order=[0], scores={0: 1.0})

    monkeypatch.setattr(search.rerank_client, "rerank", rerank)
    result = search.search(U1, "用户偏好中文")
    assert not result.hits
    assert any("变化" in note for note in result.degraded)


def test_all_malformed_extraction_is_not_successful_empty_result():
    with pytest.raises(MemoryExtractionError):
        extract._parse('{"facts":[{"kind":"profile"}]}', limit=1000)


def test_eval_purge_preserves_formal_history_and_generation(mem_env):
    formal = service.write_facts(user_id=U1, facts=[_fact("用户偏好中文")]).outcome.added[0]
    service.write_facts(user_id=U1, facts=[_fact("用户偏好中文")], scope="eval:review")
    service.clear_user(user_id=U1, scope="eval:review")
    assert service.history(user_id=U1, memory_id=formal.id)[0].new_text == formal.text
    with mem_env.db.session_scope() as session:
        assert repo.generation_of(session, U1) == 0
        assert repo.generation_of(session, U1, scope="eval:review") == 1


def test_formal_and_eval_jobs_keep_independent_generation(mem_env):
    now = mem_env.db.utc_naive()
    with mem_env.db.session_scope() as session:
        repo.enqueue_job(session, user_id=U1, kind="extract", dedupe_key="formal",
                         thread_id="t", payload={}, max_attempts=3, now=now)
        job = repo.claim_job(session, owner="formal-worker", now=now, lease_seconds=30)
        repo.purge_user(session, U1, now=now, scope="eval:review")
    with mem_env.db.session_scope() as session:
        repo.assert_claim_valid(session, user_id=U1, job_id=job.id, owner="formal-worker",
                                claim_token=job.claim_token, generation=0, now=now + timedelta(seconds=1))


def _partial_job(env, *, result: str):
    from app.memory.tables import MemoryJobRow

    job_id = _enqueue(env)
    with env.db.session_scope() as session:
        repo.claim_job(session, owner=env.worker.owner, now=env.db.utc_naive(), lease_seconds=300)
        row = session.get(MemoryJobRow, job_id)
        row.committed_at = env.db.utc_naive()
        row.stages = {"total": 2, "done": [0], "extract": {"result": result,
                       "protocol": "previous-version", "model": "previous-model"}}
    with env.db.session_scope() as session:
        return repo.get_job(session, job_id)


def test_partial_commit_restores_original_facts_after_model_change(worker_env, monkeypatch):
    first, second = "用户偏好用中文", "用户使用Python"
    service.write_facts(user_id=U1, facts=[_fact(first)])
    result = extract.dump(extract.ExtractionReport(facts=[_fact(first), _fact(second)]))
    job = _partial_job(worker_env, result=result)
    fake = _install_llm(monkeypatch, "不应调用模型")
    monkeypatch.setattr(worker_env.settings, "memory_maintenance_enabled", False)
    assert worker_env.worker._execute(job) == "succeeded"
    assert fake.calls == []
    assert {i.text for i in service.list_items(user_id=U1)["items"]} == {first, second}


def test_unrecoverable_partial_commit_is_failed_and_keeps_progress(worker_env, monkeypatch):
    job = _partial_job(worker_env, result="invalid-json")
    fake = _install_llm(monkeypatch, "不应调用模型")
    assert worker_env.worker._execute(job) == "failed"
    with worker_env.db.session_scope() as session:
        stored = repo.get_job(session, job.id)
    assert stored.status == "failed" and stored.stages["done"] == [0]
    assert stored.payload and stored.stages["extract"]["result"] == "invalid-json"
    assert fake.calls == []
