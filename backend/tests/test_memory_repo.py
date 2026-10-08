"""长期记忆的 SQL 读写层（repo）在**真库**（临时 SQLite）上的行为。

覆盖四组：
- 记忆条目：新增 / 去重 / 改写 / 软删 / 软删后可重新写入（content_hash 无唯一键的意义）；
- 审计：每个变更都恰好落一行 memory_history（ADD / UPDATE / DELETE），正文对得上；
- 任务：幂等入队、compare-and-set 认领、同用户串行、租约过期重排、退避与终态；
- 彻底删除：行删掉、审计正文脱敏、返回待清理的向量 id。

MySQL 专有行为（真列宽 / 并发认领）不在这里假装通过，见 tests/test_memory_mysql.py。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sql_text

from app.memory import repo
from app.memory.errors import MemoryConflict, MemoryLeaseLost, MemoryNotFound
from app.memory.models import (
    ACTOR_LLM, ACTOR_USER, EVENT_ADD, EVENT_DELETE, EVENT_UPDATE, JOB_FAILED, JOB_KIND_EXTRACT,
    JOB_PENDING, JOB_RUNNING, JOB_SUCCEEDED, ORIGIN_LLM, STATUS_ACTIVE, STATUS_DELETED,
    content_hash,
)

USER = "u-0000-0000-0000-000000000001"
OTHER = "u-0000-0000-0000-000000000002"


def _now() -> datetime:
    return datetime(2026, 10, 8, 12, 0, 0)


# ---- 记忆条目 ----


def test_create_item_writes_row_and_add_history(memory_db):
    with memory_db.db.session_scope() as session:
        item = repo.create_item(session, user_id=USER, text=" 用户在深圳做质检 ",
                                thread_id="qa:dev:chat:v1:u:u1:c:c1", now=_now(),
                                reason="会话提取")
    assert item.status == STATUS_ACTIVE
    assert item.origin == ORIGIN_LLM
    assert item.revision == 1
    assert item.content_hash == content_hash("用户在深圳做质检")
    assert item.embedding_version == ""            # 还没进索引
    assert item.indexed_at is None

    with memory_db.db.session_scope() as session:
        history = repo.list_history(session, USER)
    assert [h.event for h in history] == [EVENT_ADD]
    assert history[0].old_text is None
    assert history[0].new_text == " 用户在深圳做质检 "   # 审计存原文，hash 用规范化
    assert history[0].actor == ACTOR_LLM
    assert history[0].thread_id == "qa:dev:chat:v1:u:u1:c:c1"


def test_create_item_rejects_duplicate_active_text(memory_db):
    with memory_db.db.session_scope() as session:
        repo.create_item(session, user_id=USER, text="爱吃辣", now=_now())
    with pytest.raises(MemoryConflict):
        with memory_db.db.session_scope() as session:
            repo.create_item(session, user_id=USER, text="  爱吃辣  ", now=_now())  # 只差空白


def test_normalization_covers_width_and_whitespace(memory_db):
    """全角 / 连续空白这些「写法差异」按同一条事实去重（NFKC + 折叠空白）。"""
    with memory_db.db.session_scope() as session:
        repo.create_item(session, user_id=USER, text="电话是１２３", now=_now())
    with memory_db.db.session_scope() as session:
        found = repo.get_active_by_hash(session, USER, "电话是123")   # 半角数字
    assert found is not None
    assert found.text == "电话是１２３"                            # 原文不动


def test_update_item_bumps_revision_and_marks_index_dirty(memory_db):
    with memory_db.db.session_scope() as session:
        item = repo.create_item(session, user_id=USER, text="在深圳", now=_now())
        repo.mark_indexed(session, ids=[item.id], embedding_version="1", now=_now())
    with memory_db.db.session_scope() as session:
        updated = repo.update_item(session, user_id=USER, memory_id=item.id,
                                   new_text="在广州", actor=ACTOR_USER, now=_now(),
                                   reason="用户编辑")
        assert updated.text == "在广州"
        assert updated.revision == 2
        assert updated.embedding_version == ""     # 正文变了 → 旧向量作废
        assert updated.indexed_at is None

    with memory_db.db.session_scope() as session:
        history = repo.list_history(session, USER, memory_id=item.id)
    assert [h.event for h in history] == [EVENT_UPDATE, EVENT_ADD]   # 新的在前
    assert history[0].old_text == "在深圳"
    assert history[0].new_text == "在广州"
    assert history[0].actor == ACTOR_USER


def test_pending_index_ids_tracks_embedding_version(memory_db):
    with memory_db.db.session_scope() as session:
        a = repo.create_item(session, user_id=USER, text="甲", now=_now())
        b = repo.create_item(session, user_id=USER, text="乙", now=_now())
        # 「真的索引好了」= 口径 + 正文版本 + 元数据版本三者一起标上(见 mark_indexed)
        repo.mark_indexed(session, ids=[a.id], embedding_version="1", now=_now(),
                          revisions={a.id: a.revision}, meta_versions={a.id: a.meta_version})
    with memory_db.db.session_scope() as session:
        pending = repo.pending_index_ids(session, embedding_version="1")
        assert a.id not in pending and b.id in pending
        # 升级 Embedding 版本后，两条都要重写向量
        assert len(repo.pending_index_ids(session, embedding_version="2")) == 2


def test_a_mark_with_a_stale_revision_is_not_synced(memory_db):
    """迟到的索引执行者用旧版本标记：行**不许**被标成「已同步」。

    这正是「MySQL revision=2 / Qdrant revision=1 / MySQL 却说已同步」那个死角 ——
    标记按 (revision, meta_version) 做 CAS 之后，这种状态自己就会暴露成待索引。
    """
    with memory_db.db.session_scope() as session:
        item = repo.create_item(session, user_id=USER, text="偏好甲", now=_now())
        repo.mark_indexed(session, ids=[item.id], embedding_version="1", now=_now(),
                          revisions={item.id: item.revision}, meta_versions={item.id: item.meta_version})
        updated = repo.update_item(session, user_id=USER, memory_id=item.id, new_text="偏好乙",
                                   actor=ACTOR_USER, now=_now())
        assert updated.revision == 2
        # 旧执行者(仍拿着 revision=1)来标：一条都不该标上
        marked = repo.mark_indexed(session, ids=[item.id], embedding_version="1", now=_now(),
                                   revisions={item.id: item.revision},
                                   meta_versions={item.id: item.meta_version})
    assert marked == 0
    with memory_db.db.session_scope() as session:
        assert item.id in repo.pending_index_ids(session, embedding_version="1")


def test_update_and_delete_reject_foreign_or_missing_ids(memory_db):
    with memory_db.db.session_scope() as session:
        item = repo.create_item(session, user_id=USER, text="事实甲", now=_now())
    with pytest.raises(MemoryNotFound):
        with memory_db.db.session_scope() as session:
            repo.update_item(session, user_id=OTHER, memory_id=item.id, new_text="改",
                             actor=ACTOR_USER, now=_now())
    with pytest.raises(MemoryNotFound):
        with memory_db.db.session_scope() as session:
            repo.delete_item(session, user_id=USER, memory_id="0" * 32, actor=ACTOR_USER,
                             now=_now())
    # 别人的记忆对外一律「不存在」，而且没被误改
    with memory_db.db.session_scope() as session:
        assert repo.get_item(session, USER, item.id).text == "事实甲"
        assert repo.get_item(session, OTHER, item.id) is None


def test_delete_soft_deletes_and_allows_same_fact_again(memory_db):
    with memory_db.db.session_scope() as session:
        item = repo.create_item(session, user_id=USER, text="养了一只猫", now=_now())
    with memory_db.db.session_scope() as session:
        gone = repo.delete_item(session, user_id=USER, memory_id=item.id,
                                actor=ACTOR_USER, now=_now(), reason="用户删除")
        assert gone.status == STATUS_DELETED
        assert gone.deleted_at == _now()
    # 再删同一条 → 已是 deleted，判 NotFound（对外统一 404）
    with pytest.raises(MemoryNotFound):
        with memory_db.db.session_scope() as session:
            repo.delete_item(session, user_id=USER, memory_id=item.id, actor=ACTOR_USER,
                             now=_now())
    # 关键：软删的行留在表里，content_hash 上没有唯一键 —— 同一事实可以重新写入
    with memory_db.db.session_scope() as session:
        again = repo.create_item(session, user_id=USER, text="养了一只猫", now=_now())
    assert again.id != item.id
    with memory_db.db.session_scope() as session:
        assert repo.count_items(session, USER) == 1
        assert repo.count_items(session, USER, status=None) == 2
        events = [h.event for h in repo.list_history(session, USER)]
    assert events == [EVENT_ADD, EVENT_DELETE, EVENT_ADD]


def test_list_items_active_only_and_newest_first(memory_db):
    base = _now()
    with memory_db.db.session_scope() as session:
        a = repo.create_item(session, user_id=USER, text="早一点", now=base)
        b = repo.create_item(session, user_id=USER, text="晚一点",
                             now=base + timedelta(minutes=5))
        repo.delete_item(session, user_id=USER, memory_id=a.id, actor=ACTOR_USER,
                         now=base + timedelta(minutes=10))
    with memory_db.db.session_scope() as session:
        assert [i.text for i in repo.list_items(session, USER)] == ["晚一点"]
        # 含已删（管理 / 审计视角）时按 updated_at 倒序：删除动作把它顶到了最前
        assert [i.id for i in repo.list_items(session, USER, status=None)] == [a.id, b.id]


def test_active_texts_is_the_bm25_corpus(memory_db):
    with memory_db.db.session_scope() as session:
        repo.create_item(session, user_id=USER, text="甲", now=_now())
        repo.create_item(session, user_id=OTHER, text="乙", now=_now())
    with memory_db.db.session_scope() as session:
        texts = repo.active_texts(session, USER)
    assert texts == [(texts[0][0], "甲")]          # 只含本人的有效记忆


# ---- 任务 ----


def _enqueue(session, *, user_id=USER, key="k1", now=None, max_attempts=3):
    return repo.enqueue_job(session, user_id=user_id, kind=JOB_KIND_EXTRACT, dedupe_key=key,
                            thread_id=None, payload={"messages": [{"role": "user",
                                                                   "content": "你好"}]},
                            max_attempts=max_attempts, now=now or _now())


def test_enqueue_job_is_idempotent_on_dedupe_key(memory_db):
    with memory_db.db.session_scope() as session:
        first = _enqueue(session, key="round-1")
        assert first
        assert _enqueue(session, key="round-1") is None       # 同一轮重复入队不新增
        assert _enqueue(session, key="round-2")
    with memory_db.db.session_scope() as session:
        assert repo.job_counts(session, USER)[JOB_PENDING] == 2


def test_claim_is_compare_and_set_and_serializes_per_user(memory_db):
    # next_run_at 拉开先后（认领按 next_run_at 先来先到，避免并列时顺序不定）
    with memory_db.db.session_scope() as session:
        _enqueue(session, user_id=OTHER, key="u2-a", now=_now())
        _enqueue(session, user_id=USER, key="u1-a", now=_now() + timedelta(seconds=1))
        _enqueue(session, user_id=USER, key="u1-b", now=_now() + timedelta(seconds=2))

    with memory_db.db.session_scope() as session:
        first = repo.claim_job(session, owner="host:1", now=_now() + timedelta(seconds=3),
                               lease_seconds=300)
    assert first is not None
    assert first.user_id == OTHER and first.status == JOB_RUNNING and first.attempts == 1
    assert first.claim_token                                   # 每次认领都带一份凭证

    with memory_db.db.session_scope() as session:
        second = repo.claim_job(session, owner="host:1", now=_now() + timedelta(seconds=3),
                                lease_seconds=300)
    assert second is not None and second.user_id == USER          # u1-a
    assert second.claim_token != first.claim_token                # 两份认领各有各的凭证

    # 同一用户还有一条 pending，但它必须等前一条结束（维护决策按用户串行）；
    # 另一个用户已没有待办 → 这一轮什么都领不到。
    with memory_db.db.session_scope() as session:
        assert repo.claim_job(session, owner="host:1", now=_now() + timedelta(seconds=3),
                              lease_seconds=300) is None

    with memory_db.db.session_scope() as session:
        assert repo.finish_job(session, job_id=second.id, owner="host:1",
                               claim_token=second.claim_token,
                               now=_now() + timedelta(seconds=3)) is True
    with memory_db.db.session_scope() as session:
        third = repo.claim_job(session, owner="host:1", now=_now() + timedelta(seconds=3),
                               lease_seconds=300)
    assert third is not None and third.user_id == USER and third.dedupe_key == "u1-b"


def test_claim_respects_next_run_at(memory_db):
    with memory_db.db.session_scope() as session:
        job_id = _enqueue(session, key="later")
    with memory_db.db.session_scope() as session:
        job = repo.claim_job(session, owner="host:1", now=_now(), lease_seconds=300)
        assert job is not None and job.id == job_id
        status = repo.fail_job(session, job_id=job.id, owner="host:1",
                               claim_token=job.claim_token, error="超时",
                               now=_now(), backoff_seconds=30.0)
        assert status == JOB_PENDING
    with memory_db.db.session_scope() as session:            # 退避窗口内不可认领
        assert repo.claim_job(session, owner="host:1", now=_now(), lease_seconds=300) is None
    with memory_db.db.session_scope() as session:            # 30 秒后恢复可认领
        again = repo.claim_job(session, owner="host:1", now=_now() + timedelta(seconds=31),
                               lease_seconds=300)
    assert again is not None and again.attempts == 2


def test_fail_job_gives_up_after_max_attempts_and_clears_payload(memory_db):
    with memory_db.db.session_scope() as session:
        job_id = _enqueue(session, key="doomed")
    now = _now()
    for attempt in range(3):
        with memory_db.db.session_scope() as session:
            job = repo.claim_job(session, owner="host:1", now=now, lease_seconds=300)
            assert job is not None and job.id == job_id
            status = repo.fail_job(session, job_id=job_id, owner="host:1",
                                   claim_token=job.claim_token,
                                   error=f"第 {attempt + 1} 次失败",
                                   now=now, backoff_seconds=1.0)
        assert status == (JOB_FAILED if attempt == 2 else JOB_PENDING)
        now = now + timedelta(minutes=5)
    with memory_db.db.session_scope() as session:
        finished = repo.get_job(session, job_id)
    assert finished.status == JOB_FAILED
    assert finished.payload == {}                     # 终态不留对话正文
    assert finished.finished_at is not None
    assert finished.last_error == "第 3 次失败"


def test_finish_job_only_by_lease_owner(memory_db):
    with memory_db.db.session_scope() as session:
        job_id = _enqueue(session, key="lease")
    with memory_db.db.session_scope() as session:
        claimed = repo.claim_job(session, owner="host:1", now=_now(), lease_seconds=300)
    with memory_db.db.session_scope() as session:      # 别人的租约收不了尾
        assert repo.finish_job(session, job_id=job_id, owner="host:2",
                               claim_token=claimed.claim_token, now=_now()) is False
    with memory_db.db.session_scope() as session:      # 同一个人、但换了一次认领也不行
        assert repo.finish_job(session, job_id=job_id, owner="host:1", claim_token="0" * 32,
                               now=_now()) is False
    with memory_db.db.session_scope() as session:
        assert repo.finish_job(session, job_id=job_id, owner="host:1",
                               claim_token=claimed.claim_token, now=_now()) is True
        done = repo.get_job(session, job_id)
    assert done.status == JOB_SUCCEEDED
    assert done.payload == {}


def test_the_claim_token_is_what_fences_a_stale_worker(memory_db):
    """租约过期只是「允许别人接管」,不是「旧执行者获得许可」。

    接管之后旧执行者的 owner 字符串可能一模一样(重启后 host:pid 相同),只有 claim_token
    能证明「任务还是我这一次认领的」—— 续租、提交事实、收尾都必须带它。
    """
    with memory_db.db.session_scope() as session:
        job_id = _enqueue(session, key="fence")
    with memory_db.db.session_scope() as session:
        stale = repo.claim_job(session, owner="host:1", now=_now(), lease_seconds=60)
    later = _now() + timedelta(minutes=5)                    # 租约过期
    with memory_db.db.session_scope() as session:
        assert repo.requeue_expired(session, now=later) == 1
    with memory_db.db.session_scope() as session:            # 新执行者接管(owner 故意同名)
        fresh = repo.claim_job(session, owner="host:1", now=later, lease_seconds=300)
    assert fresh is not None and fresh.claim_token != stale.claim_token

    with memory_db.db.session_scope() as session:            # 旧执行者提交:必须被拦下
        with pytest.raises(MemoryLeaseLost):
            repo.assert_claim_valid(session, user_id=USER, job_id=job_id, owner="host:1",
                                    claim_token=stale.claim_token,
                                    generation=fresh.generation, now=later)
    with memory_db.db.session_scope() as session:            # 续租 / 收尾同样拦下
        assert repo.renew_lease(session, job_id=job_id, owner="host:1",
                                claim_token=stale.claim_token, now=later,
                                lease_seconds=300) is False
        assert repo.finish_job(session, job_id=job_id, owner="host:1",
                               claim_token=stale.claim_token, now=later) is False
    with memory_db.db.session_scope() as session:            # 现行执行者一切正常
        repo.assert_claim_valid(session, user_id=USER, job_id=job_id, owner="host:1",
                                claim_token=fresh.claim_token,
                                generation=fresh.generation, now=later)
        assert repo.renew_lease(session, job_id=job_id, owner="host:1",
                                claim_token=fresh.claim_token, now=later,
                                lease_seconds=300) is True


def test_an_expired_lease_fences_even_without_a_takeover(memory_db):
    """还没人接管,但租约已经过期:旧执行者同样不许提交 —— 过期不是许可。"""
    with memory_db.db.session_scope() as session:
        job_id = _enqueue(session, key="late")
    with memory_db.db.session_scope() as session:
        job = repo.claim_job(session, owner="host:1", now=_now(), lease_seconds=60)
    later = _now() + timedelta(minutes=5)
    with memory_db.db.session_scope() as session:
        with pytest.raises(MemoryLeaseLost):
            repo.assert_claim_valid(session, user_id=USER, job_id=job_id, owner="host:1",
                                    claim_token=job.claim_token,
                                    generation=job.generation, now=later)


def test_expired_lease_is_requeued(memory_db):
    with memory_db.db.session_scope() as session:
        _enqueue(session, user_id=OTHER, key="x", now=_now())
        _enqueue(session, user_id=USER, key="y", now=_now() + timedelta(seconds=1))
    with memory_db.db.session_scope() as session:
        repo.claim_job(session, owner="dead-host", now=_now() + timedelta(seconds=2),
                       lease_seconds=60)
        repo.claim_job(session, owner="dead-host", now=_now() + timedelta(seconds=2),
                       lease_seconds=60)
    later = _now() + timedelta(minutes=5)              # 租约(60s)已过期
    with memory_db.db.session_scope() as session:
        assert repo.requeue_expired(session, now=later) == 2
    with memory_db.db.session_scope() as session:
        assert repo.job_counts(session)[JOB_PENDING] == 2
        back = repo.claim_job(session, owner="host:1", now=later, lease_seconds=300)
        assert back is not None and back.attempts == 2   # 过期重排也算一次尝试
        assert back.last_error == "租约过期，重新排队"


def test_expired_lease_with_exhausted_attempts_fails(memory_db):
    with memory_db.db.session_scope() as session:
        job_id = _enqueue(session, key="one-shot", max_attempts=1)
    with memory_db.db.session_scope() as session:
        repo.claim_job(session, owner="dead-host", now=_now(), lease_seconds=60)
    later = _now() + timedelta(minutes=5)
    with memory_db.db.session_scope() as session:
        assert repo.requeue_expired(session, now=later) == 1
    with memory_db.db.session_scope() as session:
        job = repo.get_job(session, job_id)
    assert job.status == JOB_FAILED
    assert job.payload == {}
    assert job.finished_at == later


# ---- 彻底删除 ----


def test_purge_user_removes_rows_and_redacts_history(memory_db):
    with memory_db.db.session_scope() as session:
        item = repo.create_item(session, user_id=USER, text="隐私事实", now=_now())
        other = repo.create_item(session, user_id=OTHER, text="别人的事实", now=_now())
        _enqueue(session, key="p1")
    with memory_db.db.session_scope() as session:
        result = repo.purge_user(session, USER, now=_now())
    assert result.items == 1 and result.item_ids == [item.id]
    assert result.jobs == 1
    assert result.history == 1

    with memory_db.db.session_scope() as session:
        assert repo.list_items(session, USER, status=None) == []
        assert repo.job_counts(session, USER)[JOB_PENDING] == 0
        rows = session.execute(
            sql_text("SELECT old_text, new_text, reason FROM memory_history WHERE user_id = :u"),
            {"u": USER},
        ).all()
        assert rows == [(None, None, "用户数据彻底删除")]
        # 别人的数据一个字段都没动
        assert repo.get_item(session, OTHER, other.id).text == "别人的事实"


# ---- 记忆代次(generation):彻底删除的作废开关 ----


def test_generation_starts_at_zero_and_bumps_once_per_purge(memory_db):
    with memory_db.db.session_scope() as session:
        assert repo.generation_of(session, USER) == 0            # 没有行 = 0
        repo.create_item(session, user_id=USER, text="一条事实", now=_now())
        assert repo.generation_of(session, USER) == 0            # 普通写入不推进代次
    with memory_db.db.session_scope() as session:
        assert repo.bump_generation(session, USER, now=_now()) == 1
    with memory_db.db.session_scope() as session:
        assert repo.bump_generation(session, USER, now=_now()) == 2
        assert repo.generation_of(session, USER) == 2


def test_purge_bumps_the_generation_and_reports_it(memory_db):
    with memory_db.db.session_scope() as session:
        repo.create_item(session, user_id=USER, text="隐私事实", now=_now())

    with memory_db.db.session_scope() as session:
        result = repo.purge_user(session, USER, now=_now())

    assert result.generation == 1

    with memory_db.db.session_scope() as session:
        assert repo.generation_of(session, USER) == 1


def test_writes_with_a_stale_generation_are_rejected(memory_db):
    """清除之前入队 / 开始的写入,代次对不上就整批作废(事实不会被写回来)。"""
    with memory_db.db.session_scope() as session:
        item = repo.create_item(session, user_id=USER, text="原始事实", now=_now())
    with memory_db.db.session_scope() as session:
        repo.bump_generation(session, USER, now=_now())          # 用户「彻底删除」了记忆

    with memory_db.db.session_scope() as session:                # 旧任务拿着代次 0 来写
        with pytest.raises(MemoryConflict):
            repo.create_item(session, user_id=USER, text="旧任务想写回来的事实",
                             now=_now(), generation=0)
    with memory_db.db.session_scope() as session:
        with pytest.raises(MemoryConflict):
            repo.update_item(session, user_id=USER, memory_id=item.id, new_text="旧任务想改",
                             actor=ACTOR_LLM, now=_now(), generation=0)
    with memory_db.db.session_scope() as session:
        with pytest.raises(MemoryConflict):
            repo.delete_item(session, user_id=USER, memory_id=item.id, actor=ACTOR_LLM,
                             now=_now(), generation=0)

    with memory_db.db.session_scope() as session:                # 一条都没落库
        assert repo.get_item(session, USER, item.id).text == "原始事实"
    with memory_db.db.session_scope() as session:                # 代次对的写入照常
        repo.create_item(session, user_id=USER, text="新事实", now=_now(), generation=1)
        assert repo.count_items(session, USER) == 2


def test_jobs_record_the_generation_at_enqueue_time(memory_db):
    with memory_db.db.session_scope() as session:
        _enqueue(session, key="g1")
    with memory_db.db.session_scope() as session:
        repo.purge_user(session, USER, now=_now())               # 清除时会连任务一起删
    with memory_db.db.session_scope() as session:
        assert _enqueue(session, key="g2")

    with memory_db.db.session_scope() as session:
        claimed = repo.claim_job(session, owner="w1", now=_now(), lease_seconds=300)

    assert claimed is not None and claimed.dedupe_key == "g2"
    assert claimed.generation == 1                               # 入队时已 +1 的代次
