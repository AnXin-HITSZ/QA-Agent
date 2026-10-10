"""长期记忆的全部 SQL 读写：同步、短事务、一次一个 session_scope。

约定（与 app/auth/store.py、app/metering/store.py 一致）：
- 函数都是**同步**的，由 service / 工作线程经 app.memory.db.run() 丢进线程池执行；
- 一次业务操作开一个短事务，提交即结束；事务里绝不调 LLM / Embedding / Qdrant；
- 需要「检查 + 修改」原子完成的地方一律用带条件的 UPDATE（compare-and-set）；
- 排序 / 分页下推到 SQL，不做全表扫描后在 Python 里过滤。

**记忆事实只在 MySQL**：Qdrant 里的向量是派生数据，任何时候都可以从 memory_items
重建（见 service.reindex）；所以这里的写路径只管三张表，不依赖向量库的成功与否 ——
向量写失败留痕（embedding_version 保持空 / 旧值），由重建补齐。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.memory.errors import (
    MemoryConflict, MemoryLeaseLost, MemoryNotFound, MemoryStaleGeneration,
)
from app.memory.models import (
    ACTOR_LLM, EVENT_ADD, EVENT_DELETE, EVENT_UPDATE, JOB_FAILED, JOB_PENDING, JOB_RUNNING,
    JOB_SUCCEEDED, ORIGIN_LLM, SCOPE_FORMAL, STATUS_ACTIVE, STATUS_DELETED, MemoryHistory,
    MemoryItem, MemoryJob, MemoryOp, MemorySource, content_hash, normalize_text,
)
from app.memory.tables import (
    MemoryHistoryRow, MemoryItemRow, MemoryJobRow, MemoryOpRow, MemorySourceRow,
    MemoryUserStateRow,
)

# 任务租约持有者 / 错误摘要等文本列的上限（与迁移 COMMENT 一致）。
LEASE_OWNER_LIMIT = 96
JOB_ERROR_LIMIT = 500
REASON_LIMIT = 255
TURN_ID_LIMIT = 160
CLAIM_TOKEN_LIMIT = 32


def new_id() -> str:
    return uuid.uuid4().hex


def new_claim_token() -> str:
    """一次认领的凭证（fencing token）：随机、够长，不与任何其它认领相同。"""
    return uuid.uuid4().hex[:CLAIM_TOKEN_LIMIT]


# ---- 行 → 数据类 ----


def _item(row: MemoryItemRow) -> MemoryItem:
    return MemoryItem(
        id=row.id, user_id=row.user_id, text=row.text, content_hash=row.content_hash,
        status=row.status, origin=row.origin, thread_id=row.thread_id, revision=row.revision,
        embedding_version=row.embedding_version, indexed_at=row.indexed_at,
        created_at=row.created_at, updated_at=row.updated_at, deleted_at=row.deleted_at,
        scope=row.scope or SCOPE_FORMAL, kind=row.kind or "", event_time=row.event_time, fact_context=row.fact_context or {},
        generation=int(row.generation or 0), meta_version=int(row.meta_version or 0),
        indexed_revision=int(row.indexed_revision or 0),
        indexed_meta_version=int(row.indexed_meta_version or 0),
    )


def _history(row: MemoryHistoryRow) -> MemoryHistory:
    return MemoryHistory(
        id=row.id, memory_id=row.memory_id, user_id=row.user_id, event=row.event,
        old_text=row.old_text, new_text=row.new_text, actor=row.actor, reason=row.reason,
        thread_id=row.thread_id, created_at=row.created_at,
        old_context=row.old_context, new_context=row.new_context,
    )


def _job(row: MemoryJobRow) -> MemoryJob:
    return MemoryJob(
        id=row.id, user_id=row.user_id, kind=row.kind, status=row.status,
        dedupe_key=row.dedupe_key, thread_id=row.thread_id, payload=dict(row.payload or {}),
        generation=int(row.generation or 0),
        attempts=row.attempts, max_attempts=row.max_attempts, next_run_at=row.next_run_at,
        lease_owner=row.lease_owner, lease_expires_at=row.lease_expires_at,
        last_error=row.last_error, created_at=row.created_at, updated_at=row.updated_at,
        finished_at=row.finished_at,
        scope=row.scope or SCOPE_FORMAL, turn_id=row.turn_id,
        claim_token=row.claim_token or "", stages=dict(row.stages or {}),
        outcome=dict(row.outcome) if row.outcome else None, committed_at=row.committed_at,
    )


def _source(row: MemorySourceRow) -> MemorySource:
    return MemorySource(id=row.id, memory_id=row.memory_id, user_id=row.user_id,
                        scope=row.scope or SCOPE_FORMAL, conversation_id=row.conversation_id,
                        turn_id=row.turn_id, created_at=row.created_at)


def _op(row: MemoryOpRow) -> MemoryOp:
    return MemoryOp(
        id=row.id, user_id=row.user_id, scope=row.scope or SCOPE_FORMAL, kind=row.kind,
        memory_id=row.memory_id, generation=int(row.generation or 0),
        payload=dict(row.payload or {}), status=row.status, attempts=row.attempts,
        max_attempts=row.max_attempts, next_run_at=row.next_run_at,
        lease_owner=row.lease_owner, claim_token=row.claim_token or "",
        lease_expires_at=row.lease_expires_at, last_error=row.last_error,
        created_at=row.created_at, updated_at=row.updated_at, finished_at=row.finished_at,
    )


# ---- 记忆：读 ----


def get_item(session, user_id: str, memory_id: str) -> MemoryItem | None:
    row = session.get(MemoryItemRow, memory_id)
    if row is None or row.user_id != user_id:
        return None                      # 别人的记忆对外一律「不存在」
    return _item(row)


def get_active_by_hash(session, user_id: str, text: str, *, scope: str = SCOPE_FORMAL) -> MemoryItem | None:
    """同用户内按规范化正文找有效记忆（去重判断用；软删的行不参与）。"""
    row = session.execute(
        select(MemoryItemRow).where(
            MemoryItemRow.user_id == user_id,
            MemoryItemRow.scope == scope,
            MemoryItemRow.content_hash == content_hash(text),
            MemoryItemRow.status == STATUS_ACTIVE,
        ).limit(1)
    ).scalar_one_or_none()
    return _item(row) if row is not None else None


def list_items(session, user_id: str, *, status: str | None = STATUS_ACTIVE,
               limit: int = 100, offset: int = 0,
               scope: str = SCOPE_FORMAL) -> list[MemoryItem]:
    """某用户在某作用域内的记忆行（默认正式数据：评测数据不许混进面向用户的读）。"""
    q = select(MemoryItemRow).where(MemoryItemRow.user_id == user_id,
                                    MemoryItemRow.scope == scope)
    if status is not None:
        q = q.where(MemoryItemRow.status == status)
    q = q.order_by(MemoryItemRow.updated_at.desc(), MemoryItemRow.id.desc())
    rows = session.execute(q.limit(max(1, limit)).offset(max(0, offset))).scalars().all()
    return [_item(r) for r in rows]


def count_items(session, user_id: str, *, status: str | None = STATUS_ACTIVE) -> int:
    q = select(func.count()).select_from(MemoryItemRow).where(MemoryItemRow.user_id == user_id)
    if status is not None:
        q = q.where(MemoryItemRow.status == status)
    return int(session.execute(q).scalar_one())


def list_by_ids(session, ids: list[str]) -> list[MemoryItem]:
    """按 id 批量取**有效**记忆（重建索引 / 批量校验用；已删的自然缺席）。"""
    if not ids:
        return []
    rows = session.execute(
        select(MemoryItemRow).where(
            MemoryItemRow.id.in_(ids),
            MemoryItemRow.status == STATUS_ACTIVE,
        )
    ).scalars().all()
    return [_item(r) for r in rows]


def active_texts(session, user_id: str, *, limit: int | None = None,
                 scope: str = SCOPE_FORMAL) -> list[tuple[str, str]]:
    """有效记忆的 (id, 正文) 清单：BM25 语料（每次检索按用户现取，不需要缓存失效机制）。

    `limit` 是语料上限：超出时按"最近更新优先"截取,调用方(见 search.py)负责如实记降级
    —— 检索质量下降这件事不能悄悄发生。不传 limit 表示不限(仅用于测试/维护脚本)。
    `scope` 是硬过滤:关键词语料同样不许把评测数据与正式数据混在一起。
    """
    q = select(MemoryItemRow.id, MemoryItemRow.text).where(
        MemoryItemRow.user_id == user_id,
        MemoryItemRow.status == STATUS_ACTIVE,
        MemoryItemRow.scope == scope,
    ).order_by(MemoryItemRow.updated_at.desc(), MemoryItemRow.id.asc())
    if limit is not None:
        q = q.limit(max(1, int(limit)))
    return [(r[0], r[1]) for r in session.execute(q).all()]


def _index_dirty(embedding_version: str):
    """「索引与事实不一致」的 SQL 条件（三选一即脏）。

    只看 embedding_version 会漏掉最要命的一类：**同一个 Embedding 模型下正文改过、
    Qdrant 里还是旧向量**（行被标成「已索引」、搜索又能命中旧点）—— 这种状态自动修复
    永远发现不了。加上 indexed_revision / indexed_meta_version 之后，MySQL 侧自己就能
    说出「这一行到底同没同步」。
    """
    from sqlalchemy import or_

    return or_(
        MemoryItemRow.embedding_version != embedding_version,
        MemoryItemRow.indexed_revision != MemoryItemRow.revision,
        MemoryItemRow.indexed_meta_version != MemoryItemRow.meta_version,
    )


def pending_index_ids(session, *, embedding_version: str, limit: int = 500,
                      user_id: str | None = None, scope: str = SCOPE_FORMAL) -> list[str]:
    """待（重新）建索引的有效记忆 id：向量版本落后 **或** 索引标记与事实版本不符。

    换 Embedding 模型 / 索引损坏 / 正文改过而索引没跟上，都由这里发现（重建流程消费）。
    `scope` 是硬过滤：正式 Worker 只补正式数据，评测 Worker 只补自己那一个 run。
    """
    q = select(MemoryItemRow.id).where(
        MemoryItemRow.status == STATUS_ACTIVE,
        MemoryItemRow.scope == scope,
        _index_dirty(embedding_version),
    )
    if user_id is not None:
        q = q.where(MemoryItemRow.user_id == user_id)
    rows = session.execute(q.order_by(MemoryItemRow.updated_at.asc()).limit(max(1, limit))).all()
    return [r[0] for r in rows]


def count_items_in_scope(session, *, scope: str, user_id: str | None = None,
                         status: str | None = STATUS_ACTIVE) -> int:
    """某作用域（可再按用户）的有效记忆条数（索引对账用，见 service.index_drift）。"""
    q = select(func.count()).select_from(MemoryItemRow).where(MemoryItemRow.scope == scope)
    if user_id is not None:
        q = q.where(MemoryItemRow.user_id == user_id)
    if status is not None:
        q = q.where(MemoryItemRow.status == status)
    return int(session.execute(q).scalar_one())


def index_items_page(session, *, scope: str, user_id: str | None = None,
                     after: str = "", limit: int = 256) -> list[MemoryItem]:
    """按主键分页对账，既不截断旧记忆，也不一次加载全部正文。"""
    q = select(MemoryItemRow).where(MemoryItemRow.scope == scope,
                                    MemoryItemRow.status == STATUS_ACTIVE,
                                    MemoryItemRow.id > after)
    if user_id is not None:
        q = q.where(MemoryItemRow.user_id == user_id)
    return [_item(r) for r in session.execute(
        q.order_by(MemoryItemRow.id).limit(limit)).scalars().all()]


def invalidate_index_snapshots(session, items: list[MemoryItem]) -> int:
    """只清理核验失败的同一版本标记，避免每轮对账重置全部索引进度。"""
    changed = 0
    for item in items:
        result = session.execute(update(MemoryItemRow).where(
            MemoryItemRow.id == item.id, MemoryItemRow.scope == item.scope,
            MemoryItemRow.status == STATUS_ACTIVE, MemoryItemRow.revision == item.revision,
            MemoryItemRow.meta_version == item.meta_version,
            MemoryItemRow.generation == item.generation,
        ).values(embedding_version="", indexed_revision=0, indexed_meta_version=0,
                 indexed_at=None).execution_options(synchronize_session=False))
        changed += int(result.rowcount)
    return changed


def users_in_scope(session, *, scope: str) -> list[str]:
    """某作用域内出现过的用户 id（记忆行 / 任务行 / 清理台账三处取并集，去重后排序）。

    评测的 purge 拿它圈定「这次要清哪些用户」：主体可以不止一个（数据集里的 subject），
    逐个硬编码会漏，而按作用域反查是**由数据本身**决定的 —— 这一份里出现过的用户，
    一个都不落下；别的作用域的用户，一个都碰不到。
    """
    ids: set[str] = set()
    for row in (MemoryItemRow, MemoryJobRow, MemoryOpRow):
        ids.update(session.execute(
            select(row.user_id).where(row.scope == scope).distinct()
        ).scalars().all())
    return sorted(ids)
    if user_id is not None:
        q = q.where(MemoryItemRow.user_id == user_id)
    if status is not None:
        q = q.where(MemoryItemRow.status == status)
    return int(session.execute(q).scalar_one())


def clear_index_marks(session, *, scope: str, user_id: str | None = None) -> int:
    """把某作用域（可再按用户）的**索引标记整批清掉**，让所有有效记忆重新进入待索引。

    这是修复「标记说已同步、索引里其实没有/还是旧的」这类损坏的唯一入口：正常路径下
    待索引集由 _index_dirty 决定，而标记本身被写坏（例如集合被重建、点被手工删掉）时
    它永远发现不了 —— 清掉标记就能让下一次 ensure_indexed 逐条重写。
    **不删集合、不删事实**，代价是这批记忆会重新花一次 Embedding 调用。
    """
    q = update(MemoryItemRow).where(MemoryItemRow.status == STATUS_ACTIVE,
                                    MemoryItemRow.scope == scope)
    if user_id is not None:
        q = q.where(MemoryItemRow.user_id == user_id)
    res = session.execute(
        q.values(embedding_version="", indexed_revision=0, indexed_meta_version=0, indexed_at=None)
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount)


def count_pending_index(session, *, embedding_version: str, user_id: str | None = None,
                        scope: str | None = None) -> int:
    """待（重新）建索引的有效记忆条数（状态页显示「索引同步中」用，不取整批 id）。"""
    q = select(func.count()).select_from(MemoryItemRow).where(
        MemoryItemRow.status == STATUS_ACTIVE,
        _index_dirty(embedding_version),
    )
    if user_id is not None:
        q = q.where(MemoryItemRow.user_id == user_id)
    if scope is not None:
        q = q.where(MemoryItemRow.scope == scope)
    return int(session.execute(q).scalar_one())


# ---- 记忆代次（generation）：彻底删除的作废开关 ----

def generation_of(session, user_id: str, *, scope: str = SCOPE_FORMAL) -> int:
    """该用户当前的记忆代次（没有行 = 0；「彻底删除」会 +1）。"""
    row = session.get(MemoryUserStateRow, (user_id, scope))
    return int(row.generation) if row is not None else 0


def assert_generation(session, user_id: str, expected: int | None, *,
                      now: datetime | None = None, scope: str = SCOPE_FORMAL) -> int:
    """带代次写入前的比对：不一致说明期间用户「彻底删除」过，本次写入作废。

    这个读是**加行锁**的（MySQL 上是 `SELECT ... FOR UPDATE`；SQLite 忽略锁，但它的写
    本来就串行）：比对通过之后，本次事务一直握着该用户的代次行直到提交，清除那边改代次
    会被挡在门外 —— 于是「读到旧代次 → 清除 → 又写回去」这条时间线不可能成立。

    给了 `now` 时顺带把代次行建出来（首次写入 = 建行 0），这样**没有代次行**的用户
    也能被同一把锁串行化；不给就只读（不存在 = 0）。
    """
    current = lock_user_state(session, user_id, now=now, scope=scope)
    if expected is not None and int(expected) != current:
        raise MemoryStaleGeneration(
            f"记忆已被彻底删除(代次 {expected} → {current}),本次写入作废"
        )
    return current


def lock_user_state(session, user_id: str, *, now: datetime | None = None, scope: str = SCOPE_FORMAL) -> int:
    """取该用户的代次并**锁住这一行**（不存在且给了 now 就建一个代次 0 的行）。

    同一用户的并发写入 / 认领 / 清除都在这行上排队,是「同用户串行」这条保证的落点:
    跨进程的两台 worker 抢同一个用户时,行锁让第二个进来时能看到第一个已经跑着的任务。
    """
    row = _locked_state_row(session, user_id, now=now, scope=scope)
    return int(row.generation) if row is not None else 0


def _locked_state_row(session, user_id: str, *, now: datetime | None, scope: str = SCOPE_FORMAL):
    """加锁取代次行；给了 now 就先保证行存在。只在 now=None 且行不存在时返回 None。"""
    if now is None:
        return _locked_state(session, user_id, scope=scope)
    _ensure_state_row(session, user_id, now=now, scope=scope)
    row = _locked_state(session, user_id, scope=scope)
    assert row is not None, "代次行刚建出来却读不到"
    return row


def _ensure_state_row(session, user_id: str, *, now: datetime, scope: str = SCOPE_FORMAL) -> None:
    """「没有就建、有就锁住」一条语句完成（INSERT ... ON DUPLICATE KEY / DO NOTHING）。

    为什么不是「先 SELECT 没有就 INSERT」：MySQL 的 REPEATABLE READ 下，`SELECT ... FOR
    UPDATE` 查一个**不存在**的行会留下间隙锁，两个事务同时走这条路再各自 INSERT，就会
    互相等对方的间隙锁 —— 经典死锁。upsert 把「建」与「锁」合成一条语句，重复键分支
    仍然锁住已有的行，两个方向都不给对方留间隙。
    """
    values = {"user_id": user_id, "scope": scope, "generation": 0, "purged_at": None,
              "created_at": now, "updated_at": now}
    if session.get_bind().dialect.name == "mysql":
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        stmt = mysql_insert(MemoryUserStateRow).values(**values)
        # 冲突即无操作（把主键写回自己），但**仍然占住这一行的排它锁**直到提交
        session.execute(stmt.on_duplicate_key_update(user_id=stmt.inserted.user_id))
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(MemoryUserStateRow).values(**values)
        session.execute(stmt.on_conflict_do_nothing(index_elements=["user_id", "scope"]))
    session.flush()


def _locked_state(session, user_id: str, *, scope: str = SCOPE_FORMAL):
    """加行锁读代次行（不存在返回 None）。MySQL 上是 `FOR UPDATE`，SQLite 忽略。"""
    return session.execute(
        select(MemoryUserStateRow).where(MemoryUserStateRow.user_id == user_id, MemoryUserStateRow.scope == scope)
        .with_for_update()
    ).scalar_one_or_none()


def _touch_state(session, user_id: str, *, now: datetime, purge: bool = False, scope: str = SCOPE_FORMAL) -> int:
    """建立 / 推进用户的代次行（加锁）。purge=True 时代次 +1 并记 purged_at。"""
    row = _locked_state_row(session, user_id, now=now, scope=scope)
    assert row is not None
    if purge:
        row.generation = int(row.generation) + 1
        row.purged_at = now
    row.updated_at = now
    session.flush()
    return int(row.generation)


def bump_generation(session, user_id: str, *, now: datetime, scope: str = SCOPE_FORMAL) -> int:
    """「彻底删除」时把代次 +1（返回新代次）。清除之前入队的任务据此全部作废。"""
    return _touch_state(session, user_id, now=now, purge=True, scope=scope)


# ---- 记忆：写（每条变更都在同一事务里落一行 memory_history）----

def create_item(session, *, user_id: str, text: str, actor: str = ACTOR_LLM,
                origin: str = ORIGIN_LLM, thread_id: str | None = None, now: datetime,
                reason: str = "", generation: int | None = None, scope: str = SCOPE_FORMAL,
                kind: str = "", event_time: datetime | None = None,
                fact_context: dict | None = None) -> MemoryItem:
    """新增一条有效记忆 + ADD 审计。

    去重由调用方先行判断（service 层在维护决策时做）；这里再兜一道：同一用户已有
    完全相同正文的有效记忆时抛 MemoryConflict —— 并发任务在同一用户上被串行化
    （见 claim_job），这条路只该在「调用方漏判」时炸，而不是静默写重复行。

    `generation` 给定时先比对代次（见 assert_generation）：用户刚清除过记忆的话，
    这次写入作废 —— 被删掉的事实不会因为任务跑得慢又被写回来。**行上同时记下当时的
    代次**（写入后不再变）：清理由此知道该删哪些索引点（只删小于当前代次的）。

    `kind` / `event_time` 是提取协议里校验通过的值：kind 落库（不再只做校验就丢），
    event_time 未知就是 NULL（不拿 created_at 顶替 —— 记录时间与事件时间是两回事）。
    """
    current = assert_generation(session, user_id, generation, now=now, scope=scope)
    if get_active_by_hash(session, user_id, text, scope=scope) is not None:
        raise MemoryConflict("同用户已存在相同正文的有效记忆")
    row = MemoryItemRow(
        id=new_id(), user_id=user_id, text=text, content_hash=content_hash(text),
        status=STATUS_ACTIVE, origin=origin, thread_id=thread_id, revision=1,
        embedding_version="", indexed_at=None, created_at=now, updated_at=now, deleted_at=None,
        scope=scope, kind=kind or "", event_time=event_time, fact_context=fact_context or {}, generation=current,
        meta_version=0, indexed_revision=0, indexed_meta_version=0,
    )
    session.add(row)
    session.flush()                      # 拿到 id 后落审计（同一事务）
    session.add(MemoryHistoryRow(
        memory_id=row.id, user_id=user_id, scope=scope, event=EVENT_ADD, old_text=None, new_text=text,
        actor=actor, reason=(reason or "")[:REASON_LIMIT], thread_id=thread_id, created_at=now,
        new_context=row.fact_context,
    ))
    from app.memory.graph import runtime as graph_runtime
    graph_runtime.enqueue(session, _item(row), now=now)
    return _item(row)


# 「这个参数没给」与「明确给成 None」是两回事（event_time 可以为 NULL）
UNSET = object()


def update_item(session, *, user_id: str, memory_id: str, new_text: str, actor: str,
                thread_id: str | None = None, now: datetime, reason: str = "",
                expected_revision: int | None = None, generation: int | None = None,
                expected_meta_version: int | None = None,
                kind: str | None = None, event_time=UNSET, fact_context=UNSET) -> MemoryItem:
    """改写一条有效记忆的正文（Revision +1）+ UPDATE 审计；索引标记为脏（等重建补齐）。

    带条件的 UPDATE：只对「还是 active」的行生效；别人改掉的 / 已删的会被判成 NotFound。

    `expected_revision` 给定时再带一道版本条件（compare-and-set）：**维护决策是基于某个
    版本做出的**，落库时必须仍是那个版本。期间用户自己编辑过（revision 变了）就抛
    MemoryConflict —— 宁可少改一次、让下一轮重新决策，也不能把用户刚写的内容盖掉。

    `generation` 见 assert_generation：用户「彻底删除」过就整批作废，不用旧决策改新库。

    **正文没变就不重新向量化**：规范化后与现值相同（例如用户把空格改了一下、或维护决策
    给出的新写法与原值等价）时不 +revision、不清索引标记 —— 那会让一次纯元数据变更
    白花一次 Embedding 调用；只有元数据真的变了才 meta_version +1（索引层据此只刷 payload）。
    """
    addressed = session.get(MemoryItemRow, memory_id)
    if addressed is None or addressed.user_id != user_id:
        raise MemoryNotFound(memory_id)
    assert_generation(session, user_id, generation, now=now, scope=addressed.scope)
    row = session.get(MemoryItemRow, memory_id)
    if row is None or row.user_id != user_id:
        raise MemoryNotFound(memory_id)
    if expected_revision is not None and int(row.revision) != int(expected_revision):
        raise MemoryConflict(f"记忆已被改写(revision {expected_revision} → {row.revision})")
    if expected_meta_version is not None and row.meta_version != expected_meta_version:
        raise MemoryConflict("记忆元数据已变化")
    content_changed = content_hash(new_text) != row.content_hash
    meta_changed = ((kind is not None and kind != (row.kind or ""))
                    or (event_time is not UNSET and event_time != row.event_time)
                    or (fact_context is not UNSET and fact_context != row.fact_context))
    if not content_changed and not meta_changed:
        session.refresh(row)
        return _item(row)                # 什么都没变：不写审计、不动版本、不碰索引
    old_text = row.text
    conds = [MemoryItemRow.id == memory_id, MemoryItemRow.status == STATUS_ACTIVE]
    if expected_revision is not None:
        conds.append(MemoryItemRow.revision == int(expected_revision))
    if expected_meta_version is not None:
        conds.append(MemoryItemRow.meta_version == int(expected_meta_version))
    values: dict = {"updated_at": now, "meta_version": MemoryItemRow.meta_version + 1}
    if content_changed:
        values.update(text=new_text, content_hash=content_hash(new_text),
                      revision=MemoryItemRow.revision + 1,
                      embedding_version="", indexed_at=None)
    if kind is not None:
        values["kind"] = kind
    if event_time is not UNSET:
        values["event_time"] = event_time
    if fact_context is not UNSET:
        values["fact_context"] = fact_context
    res = session.execute(
        update(MemoryItemRow).where(*conds).values(**values)
        .execution_options(synchronize_session=False)
    )
    if not res.rowcount:
        if (expected_revision is not None or expected_meta_version is not None) and row.status == STATUS_ACTIVE:
            # 存在且仍有效 → 是版本对不上，而不是不存在；两种结局调用方要分开处理
            raise MemoryConflict(f"记忆 {memory_id} 的版本已变化，本次改写未生效")
        raise MemoryNotFound(memory_id)          # 已被删除 / 被并发改走
    if content_changed:
        session.add(MemoryHistoryRow(
            memory_id=memory_id, user_id=user_id, scope=row.scope, event=EVENT_UPDATE, old_text=old_text,
            new_text=new_text, old_context=row.fact_context,
            new_context=(row.fact_context if fact_context is UNSET else fact_context),
            actor=actor, reason=(reason or "")[:REASON_LIMIT],
            thread_id=thread_id, created_at=now,
        ))
    session.refresh(row)
    from app.memory.graph import runtime as graph_runtime
    graph_runtime.invalidate(session, _item(row), now=now)
    graph_runtime.enqueue(session, _item(row), now=now)
    return _item(row)


def delete_item(session, *, user_id: str, memory_id: str, actor: str,
                thread_id: str | None = None, now: datetime, reason: str = "",
                expected_revision: int | None = None,
                generation: int | None = None, expected_meta_version: int | None = None) -> MemoryItem:
    """软删一条有效记忆（status='deleted' + deleted_at）+ DELETE 审计；行与审计都保留。

    `expected_revision` 的语义同 update_item：维护决策要删的是「它当时看到的那一版」，
    版本变了就不删（用户可能刚把它改成另一件事了）。`generation` 同 create_item。
    """
    addressed = session.get(MemoryItemRow, memory_id)
    if addressed is None or addressed.user_id != user_id:
        raise MemoryNotFound(memory_id)
    assert_generation(session, user_id, generation, now=now, scope=addressed.scope)
    row = session.get(MemoryItemRow, memory_id)
    if row is None or row.user_id != user_id:
        raise MemoryNotFound(memory_id)
    if expected_revision is not None and int(row.revision) != int(expected_revision):
        raise MemoryConflict(f"记忆已被改写(revision {expected_revision} → {row.revision})")
    old_text = row.text
    conds = [MemoryItemRow.id == memory_id, MemoryItemRow.status == STATUS_ACTIVE]
    if expected_revision is not None:
        conds.append(MemoryItemRow.revision == int(expected_revision))
    if expected_meta_version is not None:
        conds.append(MemoryItemRow.meta_version == int(expected_meta_version))
    res = session.execute(
        update(MemoryItemRow)
        .where(*conds)
        .values(status=STATUS_DELETED, deleted_at=now, updated_at=now,
                embedding_version="", indexed_at=None)
        .execution_options(synchronize_session=False)
    )
    if not res.rowcount:
        if (expected_revision is not None or expected_meta_version is not None) and row.status == STATUS_ACTIVE:
            raise MemoryConflict(f"记忆 {memory_id} 的版本已变化，本次删除未生效")
        raise MemoryNotFound(memory_id)
    session.add(MemoryHistoryRow(
        memory_id=memory_id, user_id=user_id, scope=row.scope, event=EVENT_DELETE, old_text=old_text,
        new_text=None, old_context=row.fact_context, actor=actor, reason=(reason or "")[:REASON_LIMIT],
        thread_id=thread_id, created_at=now,
    ))
    session.refresh(row)
    from app.memory.graph import runtime as graph_runtime
    graph_runtime.invalidate(session, _item(row), now=now)
    return _item(row)


def mark_indexed(session, *, ids: list[str], embedding_version: str, now: datetime,
                 revisions: dict[str, int] | None = None,
                 meta_versions: dict[str, int] | None = None) -> int:
    """把一批记忆标记为「索引与事实一致」。返回受影响行数。

    给了 revisions / meta_versions（id → 本次向量化时看到的版本号）时逐条带上版本条件：
    两次索引之间正文被改过（revision 变了）或元数据被改过（meta_version 变了）的行
    **不**标已索引 —— 否则新正文会被当成「已索引」而永远等不到重建，而 Qdrant 里还挂着
    旧向量（这正是「MySQL revision=2 / Qdrant revision=1 / MySQL 却说已同步」那个死角）。
    """
    if not ids:
        return 0
    if not revisions:
        res = session.execute(
            update(MemoryItemRow).where(MemoryItemRow.id.in_(ids),
                                        MemoryItemRow.status == STATUS_ACTIVE)
            .values(embedding_version=embedding_version, indexed_at=now)
            .execution_options(synchronize_session=False)
        )
        return int(res.rowcount)
    marked = 0
    for memory_id in ids:
        # 逐条 UPDATE:必须把 id 限定到这一条 —— 批量的 in_(ids) 会让「同一 revision
        # 的多行」被每条语句重复命中,rowcount 变成 n×n,调用方按它判断「有没有全标上」
        # 就会读到假数字(2026-10-08 由 test_memory_service 的计数断言发现)。
        conds = [MemoryItemRow.id == memory_id, MemoryItemRow.status == STATUS_ACTIVE]
        want = revisions.get(memory_id)
        if want is not None:
            conds.append(MemoryItemRow.revision == int(want))
        values: dict = {"embedding_version": embedding_version, "indexed_at": now}
        if want is not None:
            values["indexed_revision"] = int(want)
        if meta_versions and memory_id in meta_versions:
            conds.append(MemoryItemRow.meta_version == int(meta_versions[memory_id]))
            values["indexed_meta_version"] = int(meta_versions[memory_id])
        res = session.execute(
            update(MemoryItemRow).where(*conds).values(**values)
            .execution_options(synchronize_session=False)
        )
        marked += int(res.rowcount)
    return marked


def mark_payload_synced(session, *, ids: list[str], now: datetime,
                        meta_versions: dict[str, int]) -> int:
    """只把「索引里的 payload 已刷新」标上（正文没变，不重新向量化）。

    向量还能用（正文与模型口径都没变），变的只是 kind / event_time 这类元数据：
    这一批索引层只调 Qdrant 的 set_payload，不调 Embedding —— 严格按 meta_version 做 CAS，
    期间又被改过（meta_version 变了）的行不标，留待下一轮。
    """
    if not ids:
        return 0
    marked = 0
    for memory_id in ids:
        want = meta_versions.get(memory_id)
        if want is None:
            continue
        conds = [MemoryItemRow.id == memory_id, MemoryItemRow.status == STATUS_ACTIVE,
                 MemoryItemRow.meta_version == int(want)]
        res = session.execute(
            update(MemoryItemRow).where(*conds)
            .values(indexed_meta_version=int(want), indexed_at=now)
            .execution_options(synchronize_session=False)
        )
        marked += int(res.rowcount)
    return marked


# ---- 审计：读 ----

def list_history(session, user_id: str, *, memory_id: str | None = None,
                 limit: int = 50, offset: int = 0, scope: str = SCOPE_FORMAL) -> list[MemoryHistory]:
    q = select(MemoryHistoryRow).where(MemoryHistoryRow.user_id == user_id, MemoryHistoryRow.scope == scope)
    if memory_id:
        q = q.where(MemoryHistoryRow.memory_id == memory_id)
    q = q.order_by(MemoryHistoryRow.id.desc())
    rows = session.execute(q.limit(max(1, limit)).offset(max(0, offset))).scalars().all()
    return [_history(r) for r in rows]


# ---- 任务：入队 / 认领 / 收尾 ----

def enqueue_job(session, *, user_id: str, kind: str, dedupe_key: str,
                thread_id: str | None, payload: dict, max_attempts: int,
                now: datetime, scope: str = SCOPE_FORMAL,
                turn_id: str | None = None) -> str | None:
    """入队一个后台任务；同一 (用户, 类型, 幂等键) 已存在时返回 None（幂等）。

    任务行记下**入队时**的记忆代次：执行到一半用户「彻底删除」过，这个任务就作废
    （不执行、不落库），被删掉的事实不会被写回来。

    代次是在**该用户的代次行锁下**读的（lock_user_state），和「彻底删除」互斥，于是
    只有两种干净结局：清除先提交 → 本任务在清除之后登记（按新代次有效）；本任务先提交
    → 它的任务行连同 payload 一起被清除的 DELETE 删掉（暂存的对话正文不会留在库里）。

    `scope` 记下这是正式数据还是某一次评测（见 models.eval_scope）：认领与补索引都按它
    过滤，评测任务不会被正式 Worker 领走，反之亦然 —— 隔离在 SQL 条件里，不靠进程开关。
    `turn_id` 是本任务对应的稳定轮次标识：执行时用它写来源关联（memory_sources）。
    """
    row = MemoryJobRow(
        id=new_id(), user_id=user_id, kind=kind, status=JOB_PENDING, dedupe_key=dedupe_key,
        thread_id=thread_id, payload=payload,
        generation=lock_user_state(session, user_id, now=now, scope=scope),
        attempts=0, max_attempts=max(1, max_attempts),
        next_run_at=now, lease_owner=None, lease_expires_at=None, last_error="",
        created_at=now, updated_at=now, finished_at=None,
        scope=scope, turn_id=(turn_id or None), claim_token="", stages={},
        outcome=None, committed_at=None,
    )
    try:
        # SAVEPOINT：撞唯一键只回滚这一条 INSERT，不带倒同一事务里已入队的别的任务
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:               # 唯一键撞了：重复入队，按幂等处理
        return None
    return row.id


def _has_running_job(session, user_id: str, *, now: datetime, scope: str = SCOPE_FORMAL) -> bool:
    """该用户此刻是否已有未过期的 running 任务（**加锁**读：MySQL 上跳过快照看最新已提交）。"""
    return session.execute(
        select(MemoryJobRow.id).where(
            MemoryJobRow.user_id == user_id,
            MemoryJobRow.scope == scope,
            MemoryJobRow.status == JOB_RUNNING,
            MemoryJobRow.lease_expires_at.is_not(None),
            MemoryJobRow.lease_expires_at > now,
        ).with_for_update().limit(1)
    ).first() is not None


def claim_job(session, *, owner: str, now: datetime, lease_seconds: float,
              kinds: tuple[str, ...] = (), scope: str = SCOPE_FORMAL) -> MemoryJob | None:
    """认领一个待执行任务（compare-and-set），没有可跑的返回 None。

    规则：
    - 只认领**本作用域**内 status='pending' 且 next_run_at <= now 的任务，按 next_run_at
      先来先到（scope 是硬过滤：正式 Worker 领不到评测任务，反之亦然）；
    - **同一用户同一时刻只允许一个 running 任务**（含租约未过期的）：维护决策要在
      「读到的现有记忆」上做，并发跑会同时 ADD 出重复行 —— 认领时显式避开这些用户；
    - 认领本身是带条件的 UPDATE（status='pending' → 'running' + 租约 + **本次认领凭证**）；
      抢输 / 已被别人拿走（rowcount=0）就换下一个候选，不覆盖别人的租约。

    「同用户只能有一个」这条**落在该用户的代次行锁上**（lock_user_state）：两台的 worker
    抢同一用户的不同任务时，后进来的那个被行锁挡在门外，等前一个提交后**加锁重读**就能
    看到它刚写下的 running 行 —— 只靠下面那个 busy_users 预筛是不够的（它是快照读，
    两台 worker 会各自读到「没有人在跑」，然后各自领走一个任务）。预筛留着只是省循环，
    不再是保证。

    **加锁顺序：先用户代次行，再任务行**（下面 lock_user_state 在 UPDATE 之前）；
    提交路径的 assert_claim_valid 保持同一顺序，两处一致才不会互等成死锁。
    """
    busy_users = set(session.execute(
        select(MemoryJobRow.user_id).where(
            MemoryJobRow.scope == scope,
            MemoryJobRow.status == JOB_RUNNING,
            MemoryJobRow.lease_expires_at.is_not(None),
            MemoryJobRow.lease_expires_at > now,
        ).distinct()
    ).scalars().all())

    q = select(MemoryJobRow.id, MemoryJobRow.user_id).where(
        MemoryJobRow.scope == scope,
        MemoryJobRow.status == JOB_PENDING,
        MemoryJobRow.next_run_at <= now,
    )
    if kinds:
        q = q.where(MemoryJobRow.kind.in_(kinds))
    candidates = session.execute(q.order_by(MemoryJobRow.next_run_at.asc()).limit(50)).all()

    for job_id, user_id in candidates:
        if user_id in busy_users:
            continue
        lock_user_state(session, user_id, now=now, scope=scope)       # 同用户在此排队(见 docstring)
        if _has_running_job(session, user_id, now=now, scope=scope):  # 加锁重读:拿到的是别人刚提交的状态
            busy_users.add(user_id)
            continue
        res = session.execute(
            update(MemoryJobRow)
            .where(MemoryJobRow.id == job_id, MemoryJobRow.status == JOB_PENDING)
            .values(status=JOB_RUNNING, lease_owner=owner[:LEASE_OWNER_LIMIT],
                    claim_token=new_claim_token(),
                    lease_expires_at=now + timedelta(seconds=max(30.0, float(lease_seconds))),
                    attempts=MemoryJobRow.attempts + 1, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount:
            session.flush()
            row = session.get(MemoryJobRow, job_id)
            return _job(row) if row is not None else None
    return None


def _claim_conds(job_id: str, owner: str, claim_token: str):
    """收尾 / 续租 / 落阶段结果的条件：**仍是自己那一次认领**。

    lease_owner 只说明「哪个 worker 身份」，claim_token 才说明「哪一次认领」——
    重启后 owner 字符串可能一模一样，只有凭证能区分。两个条件都要带。
    """
    return (MemoryJobRow.id == job_id, MemoryJobRow.status == JOB_RUNNING,
            MemoryJobRow.lease_owner == owner[:LEASE_OWNER_LIMIT],
            MemoryJobRow.claim_token == (claim_token or "")[:CLAIM_TOKEN_LIMIT])


def assert_claim_valid(session, *, user_id: str, job_id: str, owner: str, claim_token: str,
                       generation: int | None, now: datetime, scope: str = SCOPE_FORMAL) -> None:
    """**提交事实之前**在同一事务里做的 fencing 校验（失败抛 MemoryLeaseLost）。

    校验四件事：任务仍是 running、仍是自己的那一次认领（owner + claim_token）、
    租约没过期、用户代次没变。少任何一条都不许写事实 —— 这正是「旧 Worker 丢掉租约后
    照样把事实写进去」的堵漏点：租约过期只是「允许别人接管」，不是「旧执行者获得许可」。

    加锁顺序固定为「先用户代次行、再任务行」（与 claim_job 一致）。
    """
    assert_generation(session, user_id, generation, now=now, scope=scope)
    row = session.execute(
        select(MemoryJobRow).where(MemoryJobRow.id == job_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise MemoryLeaseLost("任务已不存在,本次执行作废")
    if (row.user_id != user_id or row.scope != scope or row.status != JOB_RUNNING
            or (row.lease_owner or "") != owner[:LEASE_OWNER_LIMIT]
            or (row.claim_token or "") != (claim_token or "")[:CLAIM_TOKEN_LIMIT]):
        raise MemoryLeaseLost("租约已不属于本次认领(任务已被重新认领),本次执行作废")
    if row.lease_expires_at is None or row.lease_expires_at <= now:
        raise MemoryLeaseLost("租约已过期,本次执行作废")


def renew_lease(session, *, job_id: str, owner: str, claim_token: str, now: datetime,
                lease_seconds: float) -> bool:
    """续租（**不替代**提交前的 fencing 校验）：长模型调用期间定期把租约往后推。

    条件更新，只有仍是自己那一次认领才生效；续租失败说明已经被接管，执行方必须停下。
    """
    res = session.execute(
        update(MemoryJobRow).where(*_claim_conds(job_id, owner, claim_token))
        .values(lease_expires_at=now + timedelta(seconds=max(30.0, float(lease_seconds))),
                updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def save_stages(session, *, job_id: str, owner: str, claim_token: str, stages: dict,
                now: datetime) -> bool:
    """把阶段结果写回任务行（仍是自己那一次认领才生效）。

    每次**付费阶段**一结束就写：进程被杀掉之后重试时能复用已经付过钱的那一步，
    而不是从头再调一遍模型。
    """
    res = session.execute(
        update(MemoryJobRow).where(*_claim_conds(job_id, owner, claim_token))
        .values(stages=stages, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def mark_committed(session, *, job_id: str, owner: str, claim_token: str, outcome: dict,
                   now: datetime) -> bool:
    """在**事实落库的同一事务里**记下「已提交」（仍是自己那一次认领才生效）。

    非空 committed_at 之后的重试只做收尾 / 补索引，不再执行任何提取与维护 ——
    「事实已提交但收尾失败」的重试不得重复或修改事实。
    """
    res = session.execute(
        update(MemoryJobRow).where(*_claim_conds(job_id, owner, claim_token))
        .values(committed_at=now, outcome=outcome, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def finish_job(session, *, job_id: str, owner: str, claim_token: str, now: datetime,
               outcome: dict | None = None) -> bool:
    """任务成功收尾：进 succeeded、释放租约、清空 payload（正文不在库里长留）。

    只在**仍是自己那一次认领**时生效（租约过期后任务可能已被别人重新认领）。
    stages 里的结果内容一并清掉：中间产物不与「彻底删除」的语义相悖地长期留在库里
    （摘要信息留在 outcome 里，它不含正文）。
    """
    row = session.get(MemoryJobRow, job_id)
    if row is None:
        return False
    values = {"status": JOB_SUCCEEDED, "lease_owner": None, "claim_token": "",
              "lease_expires_at": None, "payload": {}, "last_error": "",
              "finished_at": now, "updated_at": now,
              "stages": clear_stage_results(row.stages)}
    if outcome is not None:
        values["outcome"] = outcome
    res = session.execute(
        update(MemoryJobRow).where(*_claim_conds(job_id, owner, claim_token))
        .values(**values).execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def clear_stage_results(stages: dict | None) -> dict:
    """进终态时把阶段里的**内容**清掉，只留可审计的摘要（输入摘要 / 协议 / 状态）。"""
    out = {}
    for name, record in dict(stages or {}).items():
        if isinstance(record, dict):
            out[name] = {k: v for k, v in record.items() if k != "result"}
    return out


def fail_job(session, *, job_id: str, owner: str, claim_token: str, error: str, now: datetime,
             backoff_seconds: float, permanent: bool = False) -> str | None:
    """任务失败收尾：还有重试次数就退回 pending（指数退避），用尽则落 failed。

    `permanent=True` 表示「重试也不会有用」（配置缺失 / 输出结构不合法 / 输入本身有问题），
    不再消耗重试次数、直接落 failed —— 让重试次数留给真正可能自愈的失败（网络、数据库抖动）。

    返回任务的新状态（拿不到 —— 认领凭证已不属于自己 —— 返回 None）。
    """
    row = session.get(MemoryJobRow, job_id)
    if (row is None or row.status != JOB_RUNNING
            or (row.lease_owner or "") != owner[:LEASE_OWNER_LIMIT]
            or (row.claim_token or "") != (claim_token or "")[:CLAIM_TOKEN_LIMIT]):
        return None
    if permanent or row.attempts >= row.max_attempts:
        row.status = JOB_FAILED
        # 部分提交的任务保留原清单和进度供恢复，彻底清除仍会删除整行。
        if row.committed_at is None:
            row.payload = {}
            row.stages = clear_stage_results(row.stages)
        row.finished_at = now
    else:
        delay = max(1.0, float(backoff_seconds)) * (2 ** max(0, row.attempts - 1))
        row.status = JOB_PENDING
        row.next_run_at = now + timedelta(seconds=min(delay, 3600.0))
    row.last_error = (error or "")[:JOB_ERROR_LIMIT]
    row.lease_owner = None
    row.claim_token = ""
    row.lease_expires_at = None
    row.updated_at = now
    session.flush()
    return row.status


def requeue_expired(session, *, now: datetime, scope: str = SCOPE_FORMAL) -> int:
    """把**本作用域**里租约过期的 running 任务放回：重试用尽落 failed，否则退回 pending。

    进程被 kill / 机器重启后，认领过的任务靠这一步复活（任务不会自己丢）。
    只动自己作用域的：评测 Worker 不该去改写正式任务的租约状态（反之亦然）。
    """
    expired = session.execute(
        select(MemoryJobRow).where(
            MemoryJobRow.scope == scope,
            MemoryJobRow.status == JOB_RUNNING,
            MemoryJobRow.lease_expires_at.is_not(None),
            MemoryJobRow.lease_expires_at <= now,
        )
    ).scalars().all()
    for row in expired:
        if row.attempts >= row.max_attempts:
            row.status = JOB_FAILED
            if row.committed_at is None:
                row.payload = {}
                row.stages = clear_stage_results(row.stages)
            row.finished_at = now
            row.last_error = (row.last_error or "租约过期且重试用尽")[:JOB_ERROR_LIMIT]
        else:
            row.status = JOB_PENDING
            row.next_run_at = now
            row.last_error = (row.last_error or "租约过期，重新排队")[:JOB_ERROR_LIMIT]
        row.lease_owner = None
        row.claim_token = ""
        row.lease_expires_at = None
        row.updated_at = now
    if expired:
        session.flush()
    return len(expired)


def get_job(session, job_id: str) -> MemoryJob | None:
    row = session.get(MemoryJobRow, job_id)
    return _job(row) if row is not None else None


def job_counts(session, user_id: str | None = None, scope: str | None = None) -> dict[str, int]:
    """按状态计数（状态页 / 运维排查用）。给了 scope 就只数那个作用域。"""
    q = select(MemoryJobRow.status, func.count()).select_from(MemoryJobRow)
    if user_id is not None:
        q = q.where(MemoryJobRow.user_id == user_id)
    if scope is not None:
        q = q.where(MemoryJobRow.scope == scope)
    q = q.group_by(MemoryJobRow.status)
    out = {s: 0 for s in (JOB_PENDING, JOB_RUNNING, JOB_SUCCEEDED, JOB_FAILED)}
    for status, n in session.execute(q).all():
        out[str(status)] = int(n)
    return out


# ---- 来源关联（memory_sources）----


def add_source(session, *, memory_id: str, user_id: str, scope: str = SCOPE_FORMAL,
               conversation_id: str | None, turn_id: str, now: datetime) -> bool:
    """记一条「这条记忆来自哪一轮」。同一 (memory_id, turn_id) 重复登记是幂等的。

    撞唯一键只回滚这一条 INSERT（SAVEPOINT），不带倒同一事务里其它写入。
    """
    if not turn_id:
        return False
    row = MemorySourceRow(
        memory_id=memory_id, user_id=user_id, scope=scope,
        conversation_id=(conversation_id[:TURN_ID_LIMIT] if conversation_id else None),
        turn_id=turn_id[:TURN_ID_LIMIT], created_at=now,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:               # 同一轮重复登记:幂等
        return False
    return True


def add_sources(session, *, memory_ids: list[str], user_id: str, scope: str = SCOPE_FORMAL,
                conversation_id: str | None, turn_id: str, now: datetime) -> int:
    """把同一轮的若干条记忆都关联到这一轮（来源是轮次，不是单条记忆）。"""
    if not memory_ids or not turn_id:
        return 0
    return sum(add_source(session, memory_id=mid, user_id=user_id, scope=scope,
                          conversation_id=conversation_id, turn_id=turn_id, now=now)
               for mid in dict.fromkeys(memory_ids))


def list_sources(session, memory_id: str) -> list[MemorySource]:
    """某条记忆的全部来源（新的在前）。"""
    rows = session.execute(
        select(MemorySourceRow).where(MemorySourceRow.memory_id == memory_id)
        .order_by(MemorySourceRow.id.desc())
    ).scalars().all()
    return [_source(r) for r in rows]


def delete_sources_for_user(session, user_id: str, *, scope: str = SCOPE_FORMAL) -> int:
    """彻底删除时连来源关联一起删：它记的是「哪一轮说过什么」，属于用户数据。"""
    return int(session.execute(
        delete(MemorySourceRow).where(MemorySourceRow.user_id == user_id,
                                      MemorySourceRow.scope == scope)
    ).rowcount)


# ---- 删除 / 清理台账（memory_ops）----


def enqueue_op(session, *, user_id: str, kind: str, now: datetime, scope: str = SCOPE_FORMAL,
               memory_id: str | None = None, generation: int = 0,
               payload: dict | None = None, max_attempts: int = 8) -> str:
    """登记一条删除 / 清理操作（与事实写入**同一事务**），返回操作 id。

    幂等：同一目标已有 pending / running 的同类操作时复用那一行 —— 重复登记只会让
    清理跑两遍（幂等但浪费），不如在这里挡住。purge_user 按 (用户, 代次) 去重。
    """
    conds = [MemoryOpRow.user_id == user_id, MemoryOpRow.scope == scope,
             MemoryOpRow.kind == kind, MemoryOpRow.status.in_((JOB_PENDING, JOB_RUNNING))]
    if kind == "graph_sync":
        # A mutation during publication needs a new pending pass. Reusing the
        # running operation would lose it when that publisher finishes.
        conds.append(MemoryOpRow.status == JOB_PENDING)
        conds.append(MemoryOpRow.generation == int(generation))
    if kind == "purge_user":
        conds.append(MemoryOpRow.generation == int(generation))
    else:
        conds.append(MemoryOpRow.memory_id == (memory_id or ""))
    existing = session.execute(select(MemoryOpRow.id).where(*conds).limit(1)).scalar_one_or_none()
    if existing:
        return str(existing)
    op_id = new_id()
    session.add(MemoryOpRow(
        id=op_id, user_id=user_id, scope=scope, kind=kind, memory_id=memory_id,
        generation=int(generation), payload=payload or {}, status=JOB_PENDING, attempts=0,
        max_attempts=max(1, int(max_attempts)), next_run_at=now, lease_owner=None,
        claim_token="", lease_expires_at=None, last_error="", created_at=now, updated_at=now,
        finished_at=None,
    ))
    session.flush()
    return op_id


def claim_op(session, *, owner: str, now: datetime, lease_seconds: float,
             scope: str = SCOPE_FORMAL, excluded_kinds: tuple[str, ...] = ()) -> MemoryOp | None:
    """认领一条待执行的清理操作（向量删除彼此独立，不像提取任务那样需要按用户串行）。"""
    candidates = session.execute(
        select(MemoryOpRow.id).where(
            MemoryOpRow.scope == scope,
            MemoryOpRow.status == JOB_PENDING,
            MemoryOpRow.next_run_at <= now,
            MemoryOpRow.kind.not_in(excluded_kinds),
        ).order_by(MemoryOpRow.next_run_at.asc()).limit(20)
    ).all()
    for (op_id,) in candidates:
        res = session.execute(
            update(MemoryOpRow)
            .where(MemoryOpRow.id == op_id, MemoryOpRow.status == JOB_PENDING)
            .values(status=JOB_RUNNING, lease_owner=owner[:LEASE_OWNER_LIMIT],
                    claim_token=new_claim_token(),
                    lease_expires_at=now + timedelta(seconds=max(30.0, float(lease_seconds))),
                    attempts=MemoryOpRow.attempts + 1, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount:
            session.flush()
            row = session.get(MemoryOpRow, op_id)
            return _op(row) if row is not None else None
    return None


def _op_conds(op_id: str, owner: str, claim_token: str):
    return (MemoryOpRow.id == op_id, MemoryOpRow.status == JOB_RUNNING,
            MemoryOpRow.lease_owner == owner[:LEASE_OWNER_LIMIT],
            MemoryOpRow.claim_token == (claim_token or "")[:CLAIM_TOKEN_LIMIT])


def finish_op(session, *, op_id: str, owner: str, claim_token: str, now: datetime) -> bool:
    """清理成功收尾。条件更新：仍是自己那一次认领才生效。"""
    res = session.execute(
        update(MemoryOpRow).where(*_op_conds(op_id, owner, claim_token))
        .values(status=JOB_SUCCEEDED, lease_owner=None, claim_token="", lease_expires_at=None,
                last_error="", finished_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def finish_pending_op(session, *, op_id: str, now: datetime) -> bool:
    """就地收尾一条**我们自己刚刚做掉**的清理操作（不经过认领 / 凭证）。

    给「删除接口 / 事务外立刻试一次」用：台账是在事实事务里登记的，紧接着同一请求就把它
    做完了 —— 这时不需要认领，只把那一行标成成功（条件里仍要求它还没被别人认领走）。
    """
    res = session.execute(
        update(MemoryOpRow).where(MemoryOpRow.id == op_id,
                                  MemoryOpRow.status.in_((JOB_PENDING, JOB_RUNNING)))
        .values(status=JOB_SUCCEEDED, lease_owner=None, claim_token="", lease_expires_at=None,
                last_error="", finished_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def finish_pending_op_for(session, *, user_id: str, scope: str, kind: str,
                          memory_id: str | None, now: datetime) -> int:
    """按「用户 + 作用域 + 类型 + 目标」把未完成的清理操作一并收掉（幂等收尾用）。"""
    conds = [MemoryOpRow.user_id == user_id, MemoryOpRow.scope == scope,
             MemoryOpRow.kind == kind,
             MemoryOpRow.status.in_((JOB_PENDING, JOB_RUNNING))]
    conds.append(MemoryOpRow.memory_id == (memory_id or ""))
    res = session.execute(
        update(MemoryOpRow).where(*conds)
        .values(status=JOB_SUCCEEDED, lease_owner=None, claim_token="", lease_expires_at=None,
                last_error="", finished_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount)


def defer_op(session, *, op_id: str, error: str, now: datetime,
             backoff_seconds: float = 30.0) -> bool:
    """把一条还没跑成的清理操作按退避放回队列（等待 worker 重试）。"""
    res = session.execute(
        update(MemoryOpRow).where(MemoryOpRow.id == op_id,
                                  MemoryOpRow.status.in_((JOB_PENDING, JOB_RUNNING)))
        .values(status=JOB_PENDING, next_run_at=now + timedelta(seconds=max(1.0, backoff_seconds)),
                lease_owner=None, claim_token="", lease_expires_at=None,
                last_error=(error or "")[:JOB_ERROR_LIMIT], updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def fail_op(session, *, op_id: str, owner: str, claim_token: str, error: str, now: datetime,
            backoff_seconds: float = 30.0, permanent: bool = False) -> str | None:
    """清理失败：还有重试次数就退回 pending（指数退避），用尽则落 failed（对外可见）。"""
    row = session.get(MemoryOpRow, op_id)
    if (row is None or row.status != JOB_RUNNING
            or (row.lease_owner or "") != owner[:LEASE_OWNER_LIMIT]
            or (row.claim_token or "") != (claim_token or "")[:CLAIM_TOKEN_LIMIT]):
        return None
    if permanent or row.attempts >= row.max_attempts:
        row.status = JOB_FAILED
        row.finished_at = now
    else:
        delay = max(1.0, float(backoff_seconds)) * (2 ** max(0, row.attempts - 1))
        row.status = JOB_PENDING
        row.next_run_at = now + timedelta(seconds=min(delay, 3600.0))
    row.last_error = (error or "")[:JOB_ERROR_LIMIT]
    row.lease_owner = None
    row.claim_token = ""
    row.lease_expires_at = None
    row.updated_at = now
    session.flush()
    return row.status


def requeue_expired_ops(session, *, now: datetime, scope: str = SCOPE_FORMAL) -> int:
    """租约过期的清理操作放回队列（进程被杀后不会留下永远 pending 的行）。"""
    expired = session.execute(
        select(MemoryOpRow).where(
            MemoryOpRow.scope == scope,
            MemoryOpRow.status == JOB_RUNNING,
            MemoryOpRow.lease_expires_at.is_not(None),
            MemoryOpRow.lease_expires_at <= now,
        )
    ).scalars().all()
    for row in expired:
        if row.attempts >= row.max_attempts:
            row.status = JOB_FAILED
            row.finished_at = now
            row.last_error = (row.last_error or "租约过期且重试用尽")[:JOB_ERROR_LIMIT]
        else:
            row.status = JOB_PENDING
            row.next_run_at = now
            row.last_error = (row.last_error or "租约过期，重新排队")[:JOB_ERROR_LIMIT]
        row.lease_owner = None
        row.claim_token = ""
        row.lease_expires_at = None
        row.updated_at = now
    if expired:
        session.flush()
    return len(expired)


def op_counts(session, *, user_id: str | None = None, scope: str | None = None) -> dict[str, int]:
    """按状态计数（「正在清理 / 清理失败」的界面依据）。"""
    q = select(MemoryOpRow.status, func.count()).select_from(MemoryOpRow)
    if user_id is not None:
        q = q.where(MemoryOpRow.user_id == user_id)
    if scope is not None:
        q = q.where(MemoryOpRow.scope == scope)
    q = q.group_by(MemoryOpRow.status)
    out = {s: 0 for s in (JOB_PENDING, JOB_RUNNING, JOB_SUCCEEDED, JOB_FAILED)}
    for status, n in session.execute(q).all():
        out[str(status)] = int(n)
    return out


def list_ops(session, *, user_id: str | None = None, status: str | None = None,
             scope: str | None = None, limit: int = 50) -> list[MemoryOp]:
    """列出清理操作（运维 / 状态展示用；不含任何正文）。"""
    q = select(MemoryOpRow)
    if user_id is not None:
        q = q.where(MemoryOpRow.user_id == user_id)
    if status is not None:
        q = q.where(MemoryOpRow.status == status)
    if scope is not None:
        q = q.where(MemoryOpRow.scope == scope)
    rows = session.execute(q.order_by(MemoryOpRow.created_at.desc())
                           .limit(max(1, int(limit)))).scalars().all()
    return [_op(r) for r in rows]


# ---- 用户数据彻底删除（与「软删一条记忆」是两回事，见技术方案「删除语义」）----

@dataclass(frozen=True)
class PurgeResult:
    """彻底删除的结果：删掉的记忆 id（供清理向量）、新代次与各表受影响行数。"""

    item_ids: list[str]
    items: int
    jobs: int
    history: int
    generation: int = 0
    sources: int = 0
    ops: int = 0


def purge_user(session, user_id: str, *, now: datetime,
               scope: str = SCOPE_FORMAL) -> PurgeResult:
    """删掉某用户**在某作用域内**的全部记忆与其任务行；审计行保留但正文脱敏。

    **先把代次 +1 再删正文**：这样「删除期间正在跑的旧任务」在落库时会被
    assert_generation 拦下（代次对不上），不会把刚删掉的事实又写回来。
    （理论上仍有一个窗口：任务已经通过了代次比对、正在写行的同时清除开始 —— 两边
    都在同一行代次上取锁，清除拿到锁时任务那笔事务已经提交，随后被 purge 的 DELETE
    一并删掉；不会留下「清除之后又出现」的记忆。）

    连来源关联一起删（它记的是「哪一轮说过什么」）；旧的清理操作行一并收掉 ——
    本次清除用一个**带新代次**的 purge 操作代替，它只删小于新代次的点，于是清理期间
    用户新写的记忆（新代次）不会被这次清理波及。正在跑的操作行留着（删它只会让
    执行的 worker 收尾失败，而它的目标已经不在 MySQL 里，跑完也无害）。

    Qdrant 里的点不在这里删 —— 由调用方在事务外执行，失败时由 memory_ops 里的
    purge 操作按退避重试（不是只写一行日志）。

    **按 (user_id, scope) 圈定删除范围**：正式清除只删 scope='' 的行，评测清除只删该 run
    的行。评测用户本身就由 run-id 派生（跑不到真实账号上），这一层是**结构上再加一道**：
    即便有人用同一个 user_id 跑评测，purge 也删不到正式数据。
    """
    generation = bump_generation(session, user_id, now=now, scope=scope)
    item_ids = [r[0] for r in session.execute(
        select(MemoryItemRow.id).where(MemoryItemRow.user_id == user_id,
                                       MemoryItemRow.scope == scope)
    ).all()]
    items = int(session.execute(
        delete(MemoryItemRow).where(MemoryItemRow.user_id == user_id,
                                    MemoryItemRow.scope == scope)
    ).rowcount)
    jobs = int(session.execute(
        delete(MemoryJobRow).where(MemoryJobRow.user_id == user_id,
                                   MemoryJobRow.scope == scope)
    ).rowcount)
    sources = delete_sources_for_user(session, user_id, scope=scope)
    from app.memory.graph import runtime as graph_runtime
    graph_runtime.purge(session, user_id=user_id, scope=scope, generation=generation, now=now)
    ops = int(session.execute(
        delete(MemoryOpRow).where(MemoryOpRow.user_id == user_id,
                                  MemoryOpRow.scope == scope,
                                  MemoryOpRow.kind != "graph_sync",
                                  MemoryOpRow.status != JOB_RUNNING)
    ).rowcount)
    # 审计存活时间长于事实，必须按用户与作用域脱敏，不能只按当前 item_ids。
    history = int(session.execute(
        update(MemoryHistoryRow)
        .where(MemoryHistoryRow.user_id == user_id, MemoryHistoryRow.scope == scope)
        .values(old_text=None, new_text=None, old_context=None, new_context=None, reason="用户数据彻底删除")
        .execution_options(synchronize_session=False)
    ).rowcount)
    return PurgeResult(item_ids=item_ids, items=items, jobs=jobs, history=history,
                       generation=generation, sources=sources, ops=ops)
