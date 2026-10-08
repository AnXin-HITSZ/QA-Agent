"""后台提取线程(§6 / §9):认领 → 提取 → 维护 → 收尾,以及崩溃恢复与自愈重放。

线程本身不在这里起(测试直接调 run_once —— 确定性、不睡真实时间);断言的是这一轮
**做了什么**:任务状态、重试与退避、租约归属、逐用户串行、索引重放、失败不吞。

真正的多进程竞争(两台 worker 同时抢)只能在 MySQL 上验,见 tests/test_memory_mysql.py。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.config import get_settings
from app.memory import db, repo, service, vector, worker as worker_mod
from app.memory.models import (
    JOB_FAILED, JOB_KIND_EXTRACT, JOB_PENDING, JOB_RUNNING, JOB_SUCCEEDED,
)
from app.memory.worker import MemoryWorker
from tests import memorykit
from tests.memorykit import FakeLLM

U1 = "00000000-0000-4000-8000-000000000001"
U2 = "00000000-0000-4000-8000-000000000002"

MESSAGES = [{"role": "user", "content": "我以后都用 Markdown 写实验记录"},
            {"role": "assistant", "content": "好的,记下了。"}]

WORKER_PARAMS = {
    "memory_maintenance_enabled": True, "memory_maintenance_retries": 1,
    "memory_maintenance_max_actions": 5, "memory_vector_k": 3, "memory_bm25_k": 3,
    "memory_rerank_k": 3, "memory_top_k": 3, "memory_rrf_k": 60, "memory_context_chars": 2000,
    "memory_bm25_corpus_limit": 1000, "memory_rerank_max_docs": 50, "memory_rerank_max_chars": 8000,
    "memory_maintenance_vector_k": 5, "memory_maintenance_bm25_k": 5,
    "memory_maintenance_rerank_k": 5, "memory_maintenance_top_k": 4,
    "memory_worker_enabled": True, "memory_worker_tick_limit": 5,
    "memory_worker_index_batch": 10,
    "memory_job_max_attempts": 3, "memory_job_backoff_seconds": 30.0,
    "memory_job_lease_seconds": 300.0,
}


@pytest.fixture
def worker_env(memory_db, cache_env, monkeypatch):
    env = memorykit.install(monkeypatch, settings=memory_db.settings,
                            dim=memory_db.settings.embeddings_dim, params=WORKER_PARAMS)
    env.db = memory_db.db
    env.cache = cache_env
    env.worker = MemoryWorker()
    return env


def _install_llm(monkeypatch, *contents: str, error=None) -> FakeLLM:
    """把记忆用的聊天模型换成替身(提取与维护决策共用同一个工厂)。"""
    from app.memory import llm as memory_llm

    fake = FakeLLM(*contents, error=error)
    monkeypatch.setattr(memory_llm, "get_memory_llm", lambda: fake)
    return fake


def _facts_json(*texts: str, kind: str = "preference") -> str:
    return json.dumps({"facts": [{"text": t, "kind": kind} for t in texts]}, ensure_ascii=False)


def _decision_json(*events: dict) -> str:
    return json.dumps({"events": list(events), "reason": "测试"}, ensure_ascii=False)


def _enqueue(env, *, user_id: str = U1, messages=None, dedupe_key: str = "") -> str | None:
    return service.enqueue_extraction(user_id=user_id,
                                      messages=MESSAGES if messages is None else messages,
                                      thread_id="t-1", dedupe_key=dedupe_key)


def _all_jobs(session) -> list:
    """库里全部任务(按创建顺序)。列表接口本身按用户取,这里要看的是全库状态。"""
    from app.memory.tables import MemoryJobRow

    rows = session.query(MemoryJobRow).order_by(MemoryJobRow.created_at.asc()).all()
    return [repo._job(r) for r in rows]


def _job_list(env) -> list:
    with db.session_scope() as session:
        return _all_jobs(session)


def _texts(env, user_id: str = U1) -> list[str]:
    with db.session_scope() as session:
        return [item.text for item in repo.list_items(session, user_id)]


# ---- 入队 ----


def test_enqueue_is_idempotent_per_dedupe_key(worker_env):
    first = _enqueue(worker_env, dedupe_key="round-1")
    second = _enqueue(worker_env, dedupe_key="round-1")

    assert first and second is None
    assert len(_job_list(worker_env)) == 1


def test_enqueue_derives_a_key_from_the_messages(worker_env):
    assert _enqueue(worker_env) is not None
    assert _enqueue(worker_env) is None                          # 同一段对话只登记一次
    assert _enqueue(worker_env, messages=[{"role": "user", "content": "换个说法"}]) is not None


def test_enqueue_skips_empty_or_system_only_messages(worker_env):
    assert _enqueue(worker_env, messages=[]) is None
    assert _enqueue(worker_env, messages=[{"role": "system", "content": "你是助手"}]) is None
    assert _job_list(worker_env) == []


def test_enqueue_is_silent_when_memory_is_off(worker_env, monkeypatch):
    monkeypatch.setattr(worker_env.settings, "memory_enabled", False)

    assert _enqueue(worker_env) is None
    assert _job_list(worker_env) == []


# ---- 一轮:成功路径 ----


def test_a_claimed_job_extracts_and_writes_memories(worker_env, monkeypatch):
    _enqueue(worker_env)
    fake = _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"))

    stats = worker_env.worker.run_once()

    assert stats["claimed"] == 1 and stats["succeeded"] == 1 and stats["failed"] == 0
    assert _texts(worker_env) == ["用户偏好用 Markdown 写实验记录"]
    job = _job_list(worker_env)[0]
    assert job.status == JOB_SUCCEEDED and job.payload == {}      # 终态不留正文
    assert job.last_error == ""
    assert len(fake.calls) == 1


def test_worker_status_reports_queue_and_counters(worker_env, monkeypatch):
    _enqueue(worker_env)
    _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"))
    worker_env.worker.run_once()

    status = worker_env.worker.status()

    assert status["jobs"][JOB_SUCCEEDED] == 1
    assert status["succeeded"] == 1 and status["claimed"] == 1
    assert status["running"] is False                             # 测试没起线程
    assert status["configured"] is True and status["enabled"] is True


def test_an_empty_queue_replays_pending_indexes(worker_env, monkeypatch):
    """索引写失败留下的行,靠空闲轮次自愈 —— 不需要人肉跑脚本。"""
    _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"))
    flaky = {"down": True}
    real_upsert = vector.upsert

    def upsert(points):
        if flaky["down"]:
            raise RuntimeError("qdrant 写不进去")
        return real_upsert(points)

    monkeypatch.setattr(vector, "upsert", upsert)
    _enqueue(worker_env)
    worker_env.worker.run_once()                                  # 这一轮索引失败:行留着待索引
    assert _job_list(worker_env)[0].status == JOB_SUCCEEDED

    with db.session_scope() as session:
        pending = repo.pending_index_ids(session, embedding_version=service.embedding_version(),
                                         limit=10)
    assert pending                                                       # 确实有待索引的行

    flaky["down"] = False
    stats = worker_env.worker.run_once(force=True)       # force:手动触发不看退避

    assert stats["claimed"] == 0 and stats["indexed"] == len(pending)


def test_pausing_auto_write_still_replays_pending_indexes(worker_env, monkeypatch):
    """暂停自动写入时:领任务停,但**索引重放照跑**(派生数据维护,不产生新记忆)。

    否则暂停期间已有记忆会一直停在「待补索引」——那既不是暂停写入要停的东西,
    恢复之后也白等一轮。
    """
    _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"))
    flaky = {"down": True}
    real_upsert = vector.upsert

    def upsert(points):
        if flaky["down"]:
            raise RuntimeError("qdrant 写不进去")
        return real_upsert(points)

    monkeypatch.setattr(vector, "upsert", upsert)
    _enqueue(worker_env)
    worker_env.worker.run_once()                                  # 索引失败:行留着待索引
    with db.session_scope() as session:
        pending = repo.pending_index_ids(session, embedding_version=service.embedding_version(),
                                         limit=10)
    assert pending

    flaky["down"] = False
    monkeypatch.setattr(worker_env.settings, "memory_write_enabled", False)
    stats = worker_env.worker.run_once(force=True)

    assert stats["claimed"] == 0 and stats["indexed"] == len(pending)
    assert "已暂停" in stats["skipped"]


def test_index_replay_backs_off_while_the_endpoint_is_down(worker_env, monkeypatch):
    _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"))
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("embedding 端点 500")

    monkeypatch.setattr(service, "get_embeddings", boom)
    _enqueue(worker_env)
    worker_env.worker.run_once()
    first = calls["n"]

    worker_env.worker.run_once()                  # 退避中:不再敲打付费端点

    assert first >= 1 and calls["n"] == first


# ---- 一轮:失败与恢复 ----


def test_a_structural_error_fails_the_job_without_burning_retries(worker_env, monkeypatch):
    _enqueue(worker_env)
    _install_llm(monkeypatch, "我想了想,这条不用记。")

    stats = worker_env.worker.run_once()

    assert stats["failed"] == 1
    job = _job_list(worker_env)[0]
    assert job.status == JOB_FAILED and job.attempts == 1         # 重试对这个输入没有意义
    assert job.payload == {} and "无法解析" in job.last_error
    assert _texts(worker_env) == []


def test_a_transient_error_returns_the_job_to_pending_with_backoff(worker_env, monkeypatch):
    _enqueue(worker_env)
    _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"))

    def boom(**kwargs):
        raise RuntimeError("mysql 连接被重置")

    monkeypatch.setattr(service, "apply_facts", boom)

    stats = worker_env.worker.run_once()

    assert stats["failed"] == 1
    job = _job_list(worker_env)[0]
    assert job.status == JOB_PENDING and job.attempts == 1
    assert job.last_error == "RuntimeError"           # 外来异常只留类名(消息可能带 SQL 参数)
    assert job.next_run_at > db.utc_naive()


def test_a_job_without_a_payload_fails_permanently(worker_env):
    with db.session_scope() as session:
        repo.enqueue_job(session, user_id=U1, kind=JOB_KIND_EXTRACT, dedupe_key="k",
                         thread_id=None, payload={}, max_attempts=3, now=db.utc_naive())

    worker_env.worker.run_once()

    job = _job_list(worker_env)[0]
    assert job.status == JOB_FAILED and "没有可提取" in job.last_error


# ---- 用户「彻底删除」之后:旧任务作废(代次) ----


def test_a_job_enqueued_before_the_clear_never_runs(worker_env, monkeypatch):
    """清除记忆之前的任务:连任务行都一起删掉(含 payload 里的对话正文),不会再被认领。"""
    _enqueue(worker_env)
    fake = _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"))
    service.clear_user(user_id=U1)

    stats = worker_env.worker.run_once()

    assert stats["claimed"] == 0
    assert _job_list(worker_env) == []           # 排队中的任务一并清除,不留待执行
    assert fake.calls == []                      # 一次模型调用都没花
    assert _texts(worker_env) == []


def test_a_claimed_job_is_voided_when_the_user_clears_before_it_starts(worker_env, monkeypatch):
    """竞态:任务已被认领(拿到租约),用户在它开跑之前点了「彻底删除」。

    这条最危险 —— 没有代次校验的话,它醒来就会把刚被删掉的事实重新提取、重新写回来。
    """
    _enqueue(worker_env)
    fake = _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"))
    with db.session_scope() as session:
        job = repo.claim_job(session, owner=worker_env.worker.owner, now=db.utc_naive(),
                             lease_seconds=300)
    assert job is not None and job.generation == 0

    service.clear_user(user_id=U1)               # 执行途中清除

    assert worker_env.worker._execute(job) == "failed"
    assert fake.calls == []                      # 代次校验在提取之前,不白花调用
    assert _texts(worker_env) == []


def test_a_write_voided_midway_is_a_permanent_failure_not_a_retry(worker_env, monkeypatch):
    """竞态:任务已通过执行前的代次校验,落库时代次才对不上。

    这时**不能**退回重试队列:结果不会变,只会一次次白花模型调用(§10 不宣称付费调用
    可重放;重试要有意义才重试)。
    """
    from app.memory.errors import MemoryStaleGeneration

    _enqueue(worker_env)
    _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"))

    def cleared_underneath(**kwargs):
        raise MemoryStaleGeneration("记忆已被彻底删除(代次 0 → 1),本次写入作废")

    monkeypatch.setattr(service, "apply_facts", cleared_underneath)

    stats = worker_env.worker.run_once()

    assert stats["failed"] == 1
    job = _job_list(worker_env)[0]
    assert job.status == JOB_FAILED and job.attempts == 1     # 不是 pending
    assert "清除" in job.last_error


def test_an_expired_lease_is_recovered_and_the_job_completes(worker_env, monkeypatch):
    """进程被 kill 之后:任务不会消失,租约一过期就被放回并重新跑完。"""
    _enqueue(worker_env)
    with db.session_scope() as session:
        job = repo.claim_job(session, owner="dead-worker", now=db.utc_naive(), lease_seconds=300)
        from app.memory.tables import MemoryJobRow

        row = session.get(MemoryJobRow, job.id)
        row.lease_expires_at = db.utc_naive() - timedelta(seconds=1)     # 假装上一轮已经死了
    _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"))

    stats = worker_env.worker.run_once()

    assert stats["recovered"] == 1 and stats["succeeded"] == 1
    assert _job_list(worker_env)[0].status == JOB_SUCCEEDED


def test_a_taken_over_job_cannot_be_committed_by_the_stale_executor(worker_env, monkeypatch):
    """租约在调模型的当口凉了、任务被 B 接管:A 回来提交时**在事务里**被拦下,一个字都不写。

    续租只是「尽量别让租约在调用模型时凉掉」,真正的闸门是提交事实前的 fencing 校验。
    A 走的是 _execute 直接调用(测试不起续租线程),所以这里考的正是那道闸门本身。
    """
    from app.memory.tables import MemoryJobRow

    _enqueue(worker_env)
    fake = _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"))
    with db.session_scope() as session:                    # A 认领:此刻租约有效
        job_a = repo.claim_job(session, owner=worker_env.worker.owner, now=db.utc_naive(),
                               lease_seconds=300)
    assert job_a is not None
    b = MemoryWorker()
    stolen: dict = {}
    real_invoke = fake.invoke

    def invoke(messages, **kwargs):
        out = real_invoke(messages, **kwargs)              # A 的提取调用正在进行……
        if not stolen:
            with db.session_scope() as session:            # 租约凉了(A 此刻不持有任何事务)
                session.get(MemoryJobRow, job_a.id).lease_expires_at = (
                    db.utc_naive() - timedelta(seconds=1))
            with db.session_scope() as session:            # B:恢复过期租约 → 接管
                assert repo.requeue_expired(session, now=db.utc_naive()) == 1
            with db.session_scope() as session:
                stolen["job"] = repo.claim_job(session, owner=b.owner, now=db.utc_naive(),
                                               lease_seconds=300)
        return out

    monkeypatch.setattr(fake, "invoke", invoke)

    assert worker_env.worker._execute(job_a) == "failed"   # A 的提交被拦下
    assert _texts(worker_env) == []                        # 没有写出任何记忆
    job = _job_list(worker_env)[0]
    assert job.status == JOB_RUNNING and job.lease_owner == b.owner
    assert job.committed_at is None                        # A 连「已提交」都没能标上

    assert b._execute(stolen["job"]) == "succeeded"        # B 拿着自己的凭证接着做完
    assert _texts(worker_env) == ["用户偏好用 Markdown 写实验记录"]
    assert _job_list(worker_env)[0].status == JOB_SUCCEEDED


def test_lease_renewal_tells_a_lost_lease_apart_from_a_dead_database(worker_env, monkeypatch):
    """续租的三种结局必须分清:续上 / 数据库抖动(**不**算丢租)/ 认领已不成立(停手)。

    把数据库抖动判成「失去租约」会让正常执行白停;把「已被接管」当成没续上接着跑,
    就会写出一个已被别人接管的任务的结果 —— 两种错都真实存在,所以三种结局一起测。
    """
    from app.memory.tables import MemoryJobRow
    from app.memory.worker import _LeaseGuard

    _enqueue(worker_env)
    with db.session_scope() as session:
        job = repo.claim_job(session, owner=worker_env.worker.owner, now=db.utc_naive(),
                             lease_seconds=300)
    guard = _LeaseGuard(worker_env.worker, job)
    down = {"yes": False}
    real_scope = db.session_scope

    def scope():
        if down["yes"]:
            raise RuntimeError("mysql 连不上")
        return real_scope()

    monkeypatch.setattr(db, "session_scope", scope)

    assert guard._renew() is True                          # 正常续上
    assert guard.renewals == 1 and not guard.lost.is_set()

    down["yes"] = True
    assert guard._renew() is True                          # 这一次没续上,但**没**判定丢租
    assert guard.renewals == 1 and not guard.lost.is_set()
    down["yes"] = False

    with db.session_scope() as session:                    # 租约过期 → B 接管
        session.get(MemoryJobRow, job.id).lease_expires_at = (
            db.utc_naive() - timedelta(seconds=1))
    with db.session_scope() as session:
        assert repo.requeue_expired(session, now=db.utc_naive()) == 1
    with db.session_scope() as session:
        assert repo.claim_job(session, owner="worker-b", now=db.utc_naive(),
                              lease_seconds=300) is not None

    assert guard._renew() is False                         # 明确:这份认领已不成立
    assert guard.renewals == 1 and not guard.lost.is_set()

    guard._loop(0.02)                                      # 后台轮询也立刻停手
    assert guard.lost.is_set() and guard.renewals == 1


# ---- 阶段结果:重试不重复付费、已提交不重复改事实 ----


def test_a_retry_reuses_the_paid_extraction(worker_env, monkeypatch):
    """落库失败退回重试:提取已经付过费,重试**从落库接着做**,不再调一次模型。"""
    from app.memory.tables import MemoryJobRow

    _enqueue(worker_env)
    fake = _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"))
    real_apply = service.apply_facts
    boom = {"n": 0}

    def flaky(**kwargs):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("mysql 连接被重置")
        return real_apply(**kwargs)

    monkeypatch.setattr(service, "apply_facts", flaky)

    first = worker_env.worker.run_once()

    assert first["failed"] == 1
    job = _job_list(worker_env)[0]
    assert job.status == JOB_PENDING and job.attempts == 1
    assert job.stages[service.STAGE_EXTRACT]["facts"] == 1   # 提取结果存在任务行上
    assert job.stages[service.STAGE_EXTRACT]["status"] == "ok"
    assert len(fake.calls) == 1                              # 这一轮只付了一次费
    assert _texts(worker_env) == []

    with db.session_scope() as session:                      # 把退避推到期,不等真实时间
        for row in session.query(MemoryJobRow).all():
            row.next_run_at = db.utc_naive()
    second = worker_env.worker.run_once()

    assert second["succeeded"] == 1
    assert len(fake.calls) == 1                              # 重试没有再调模型
    assert _texts(worker_env) == ["用户偏好用 Markdown 写实验记录"]


def test_a_retry_after_the_facts_are_committed_only_finalizes(worker_env, monkeypatch):
    """committed_at 非空之后的收尾重试:只补索引 + 收尾,**绝不重新提取、不改已落库的事实**。

    这是最容易被写错的一条:重试路径如果无脑从第一步开始,就会把已经生效的事实再写一遍
    (而且再花一次模型调用)。所以断言两件事:零模型调用、事实数量不变。
    """
    from app.memory.extract import ExtractedFact
    from app.memory.tables import MemoryJobRow

    text = "用户偏好用 Markdown 写实验记录"
    real_upsert = vector.upsert
    monkeypatch.setattr(vector, "upsert",
                        lambda points: (_ for _ in ()).throw(RuntimeError("qdrant 写不进去")))
    service.write_facts(user_id=U1, facts=[ExtractedFact(text=text, kind="preference")])
    monkeypatch.setattr(vector, "upsert", real_upsert)       # 索引层恢复:留下一条待补索引的行

    _enqueue(worker_env)
    fake = _install_llm(monkeypatch, _facts_json("这条不该被再次提取"))
    worker = worker_env.worker
    with db.session_scope() as session:                      # 认领 → 事实已提交 → 收尾前被杀
        job = repo.claim_job(session, owner=worker.owner, now=db.utc_naive(), lease_seconds=300)
        assert repo.mark_committed(session, job_id=job.id, owner=worker.owner,
                                   claim_token=job.claim_token, outcome={"added": 1},
                                   now=db.utc_naive())
        session.get(MemoryJobRow, job.id).stages = {"total": 1, "done": [0]}
        session.get(MemoryJobRow, job.id).lease_expires_at = (
            db.utc_naive() - timedelta(seconds=1))

    stats = worker.run_once()

    assert stats["recovered"] == 1 and stats["succeeded"] == 1
    assert fake.calls == []                                  # 一次模型调用都没花
    job = _job_list(worker_env)[0]
    assert job.status == JOB_SUCCEEDED and job.committed_at is not None
    assert job.outcome == {"finalized_only": True, "index_deferred": 0}
    assert _texts(worker_env) == [text]                      # 事实没有被再写一遍
    assert vector.count(user_id=U1) == 1                     # 收尾顺带把待补索引的那条补上了


# ---- 清理台账:删除的向量收尾不靠「写一行日志」----


def test_a_failed_vector_cleanup_is_replayed_by_the_worker(worker_env, monkeypatch):
    """删除时清向量失败:台账留在库里(带退避),空闲轮次把它做完 —— 残留点不会一直躺着。"""
    from app.memory.extract import ExtractedFact

    item = service.write_facts(
        user_id=U1, facts=[ExtractedFact(text="待删除的偏好", kind="preference")]).outcome.added[0]
    assert vector.count(user_id=U1) == 1
    real_delete = vector.delete_ids
    down = {"yes": True}

    def flaky(ids, **kwargs):
        if down["yes"]:
            raise RuntimeError("qdrant 不可达")
        return real_delete(ids, **kwargs)

    monkeypatch.setattr(vector, "delete_ids", flaky)
    out = service.delete_item(user_id=U1, memory_id=item.id)

    assert out["cleanup"] == "pending" and out["vector_cleaned"] is False
    assert _texts(worker_env) == []                          # 事实已经删掉(接口立刻生效)
    assert vector.count(user_id=U1) == 1                     # 但向量点还在:残留
    with db.session_scope() as session:                      # 台账留在库里,而不是只写日志
        ops = repo.list_ops(session, user_id=U1)
    assert [op.kind for op in ops] == ["delete_vector"]
    assert [op.status for op in ops] == [JOB_PENDING]

    down["yes"] = False
    stats = worker_env.worker.run_once()

    assert stats["ops"] == 1 and vector.count(user_id=U1) == 0
    with db.session_scope() as session:
        assert [op.status for op in repo.list_ops(session, user_id=U1)] == [JOB_SUCCEEDED]


def test_retries_are_exhausted_into_a_failed_terminal_state(worker_env, monkeypatch):
    _enqueue(worker_env)
    _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"))
    monkeypatch.setattr(service, "apply_facts",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    for _ in range(3):                                   # max_attempts=3,且每轮都忽略退避
        worker_env.worker.run_once(force=True)
        with db.session_scope() as session:              # 手动把退避推到期,不等真实时间
            from app.memory.tables import MemoryJobRow

            for row in session.query(MemoryJobRow).all():
                row.next_run_at = db.utc_naive()

    job = _job_list(worker_env)[0]
    assert job.status == JOB_FAILED and job.attempts == 3
    assert job.last_error == "RuntimeError"


def test_two_jobs_of_the_same_user_run_one_at_a_time(worker_env, monkeypatch):
    """维护决策要在「读到的现有记忆」上做:同一用户并发跑会同时 ADD 出重复行。"""
    _enqueue(worker_env, dedupe_key="k1")
    _enqueue(worker_env, dedupe_key="k2")
    # 第 1 个任务:提取(还没有候选,不花决策调用);第 2 个任务:提取 + 决策(候选非空)
    _install_llm(monkeypatch, _facts_json("用户偏好用 Markdown 写实验记录"),
                 _facts_json("用户偏好用 Markdown 写实验记录"),
                 _decision_json({"event": "NONE"}))
    running_seen: list[int] = []
    real = service.apply_facts

    def spy(**kwargs):
        with db.session_scope() as session:      # 执行中:全库同时处于 running 的任务数
            running_seen.append(sum(1 for j in _all_jobs(session) if j.status == JOB_RUNNING))
        return real(**kwargs)

    monkeypatch.setattr(service, "apply_facts", spy)

    stats = worker_env.worker.run_once()

    assert stats["claimed"] == 2 and stats["succeeded"] == 2
    assert running_seen == [1, 1]                        # 两次执行都是独占的,没有并存


def test_a_database_outage_does_not_raise_and_backs_off(worker_env, monkeypatch):
    def boom():
        raise RuntimeError("mysql 连不上")

    monkeypatch.setattr(db, "session_scope", boom)

    first = worker_env.worker.run_once()
    second = worker_env.worker.run_once()                # 退避中:直接跳过,不反复敲门

    assert first == {"error": "RuntimeError"}
    assert second == {"skipped": "数据库退避中"}
    assert worker_env.worker.status()["db_ok"] is False
    # 运维手动触发可以强行试一次
    assert worker_env.worker.run_once(force=True) == {"error": "RuntimeError"}


def test_owner_identity_is_stable_within_a_worker(worker_env):
    assert worker_env.worker.owner == worker_env.worker.owner
    assert worker_env.worker.owner != MemoryWorker().owner


def test_start_does_nothing_when_disabled(worker_env, monkeypatch):
    monkeypatch.setattr(worker_env.settings, "memory_worker_enabled", False)

    worker_env.worker.start()

    assert worker_env.worker.status()["running"] is False


def test_start_and_stop_are_idempotent(worker_env):
    worker_env.worker.start()
    worker_env.worker.start()                            # 第二次不该起第二个线程
    try:
        assert worker_env.worker.status()["running"] is True
    finally:
        worker_env.worker.stop()
    worker_env.worker.stop()                             # 重复关停不报错
    assert worker_env.worker.status()["running"] is False


def test_module_helpers_use_a_singleton(worker_env):
    assert worker_mod.get_worker() is worker_mod.get_worker()
    worker_mod.install_worker(worker_env.worker)
    try:
        assert worker_mod.get_worker() is worker_env.worker
    finally:
        worker_mod.install_worker(None)


def test_settings_carry_the_worker_defaults():
    s = get_settings()
    assert s.memory_worker_interval_seconds > 0
    assert s.memory_worker_index_batch >= 1
    assert s.memory_job_max_attempts >= 1 and s.memory_job_lease_seconds > 0
