"""长期记忆的后台 worker:认领任务 → 提取 → 维护落库 → 收尾(§6 / §9)。

为什么要有独立线程:提取与维护都是**付费的模型调用 + 若干次网络往返**,绝不能挂在
聊天请求上等它跑完(用户等的是回答,不是记忆)。任务本身持久化在 MySQL,进程崩了、
租约过期了都能被重新认领 —— 线程只是执行者,不是事实源。

线程模型(与 app/metering/writer.py 同一套路子):
- 每进程一个守护线程、懒启动;启动失败不影响应用(lifespan 里包了 try/except);
- 一轮的结构是固定的:**先恢复过期租约 → 认领(每用户同时只跑一个)→ 逐个执行 → 收尾**;
  认领本身是带条件的 UPDATE,多 worker 抢同一个任务只有一个成功;
- **执行期间不持有任何数据库事务**:事务只包住「认领」与「收尾」两次短操作,提取 /
  向量化都在事务外(与 service 层同一条铁律);
- 认领不到任务时才顺手补一轮「待索引重放」:索引写失败留下的行靠这里自愈,
  不需要人肉跑脚本;embedding 端点持续不可用时按退避跳过,不反复花钱;
- 数据库不可用时按退避重试,不阻塞启动、不阻塞请求。

**作用域(scope)**:worker 构造时带一个作用域,认领任务、恢复过期租约、补索引、跑清理
台账全都只认这一个作用域 —— 正式 Worker 只碰 `scope=''` 的数据,评测 Worker 只碰
`'eval:<run-id>'` 那一份(见 models.eval_scope)。隔离落在 SQL 条件里,不靠「运行时记得
别开正式 Worker」。

**fencing(续租与提交校验)**:每个任务被认领时会拿到一份 claim_token。提交事实、续租、
写阶段结果、收尾都带上它(见 repo.assert_claim_valid):**租约过期只是「允许别人接管」,
不是「旧执行者获得许可」**。长模型调用期间由守护线程定期续租(见 _LeaseGuard),续租失败
就停手;但续租只是「尽量别让租约在调用模型时凉掉」,它**不替代**提交前的那道校验 ——
两道都必须在。

**阶段结果(stages)**:提取结果、维护决策随事实**同一个事务**落库(见 service 的 STAGE_*),
进程被杀之后重试能复用已经付过费的调用,而不是从头再调一遍模型;事实全部提交后只收尾；部分提交按原提取清单继续未完成事实，不重新提取。

**清理台账(memory_ops)**:删除 / 彻底清除留下的向量清理在这里按退避重试 —— 删除的收尾
不再只是写一行日志。空闲轮次分页**逐条**核验每条有效记忆的当前版本点与 payload(不是只比总点数),
并登记迟到孤儿点 / 旧版本点的清理;发现漂移就**选择性**清除对不上的那些同步标记再补写 ——
那是**唯一**能发现「标记说已同步、点却不在集合里」的自动路径
(`MEMORY_WORKER_INDEX_REPAIR=false` 时连这条核验也不跑)。

日志口径(§7):**绝不把记忆正文写进日志**。失败原因只记「异常类名 + 我们自己写的说明」,
外来异常(SQLAlchemy / httpx…)连消息都不记 —— 它们的 str() 可能带着 SQL 参数(即正文)。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Iterable

from app.config import get_settings
from app.memory import db, repo, service, vector
from app.memory.errors import (
    MemoryExtractionError, MemoryLeaseLost, MemoryNotConfigured, MemoryStaleGeneration,
)
from app.memory.models import (
    JOB_KIND_EXTRACT, OP_DELETE_VECTOR, OP_DELETE_OLD_VERSIONS, OP_PURGE_USER, SCOPE_FORMAL, fingerprint,
)
from app.memory.service import STAGE_DONE, STAGE_EXTRACT, STAGE_TOTAL

logger = logging.getLogger(__name__)

DB_BACKOFF_START = 5.0
DB_BACKOFF_MAX = 300.0
INDEX_BACKOFF_START = 60.0      # 索引重放失败(embedding 端点不可用)后的退避基数
INDEX_BACKOFF_MAX = 900.0
TICK_CAP = 50                   # 单轮最多认领几个任务(配置为「不限」时的硬上限)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _messages_digest(messages: list[dict], note: str = "") -> str:
    """这次输入的摘要(提取阶段的复用判据:同一份对话才认上一轮的结果)。

    `note`(评测适配说明,见 service.enqueue_extraction)非空时参与摘要:换了说明就不是
    同一份输入,不复用旧结果。空串不加入摘要 —— 正式路径的既有阶段结果照旧可复用。
    """
    parts = [f"{m.get('role')}:{m.get('content')}" for m in messages]
    if note:
        parts.append(f"note:{note}")
    return fingerprint(*parts)


def _done_indices(stages: dict | None) -> list[int]:
    """阶段结果里「已经提交过」的输入下标(重试时按它跳过,不重复改事实)。"""
    raw = (stages or {}).get(STAGE_DONE) or []
    return [int(i) for i in raw if isinstance(i, int)]


def _merge_done(stages: dict, done: Iterable[int]) -> None:
    """把这一轮的结果并入阶段里的「已提交」清单(同一条事实只记一次)。"""
    current = stages.setdefault(STAGE_DONE, [])
    for index in done:
        if int(index) not in current:
            current.append(int(index))


class _LeaseGuard:
    """执行期间的租约守护:后台线程定期续租,长模型调用不会把租约放凉。

    三种结局分得很清(这是「旧执行者丢掉租约还照样写库」的正面堵法):
    - 续租成功 → 继续跑;
    - 续租返回「这份认领已不成立」(任务被别人接管 / 凭证被换)→ 置 lost,执行方停手;
    - 数据库抖动(异常)→ 只是这一次没续上,下一次再试 —— **不**据此判定失去租约,
      真正的闸门是提交事实时那道 fencing 校验(见 repo.assert_claim_valid)。
    """

    def __init__(self, worker: "MemoryWorker", job) -> None:
        self._worker = worker
        self._job = job
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = threading.Event()
        self.renewals = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, args=(self._interval(),),
                                        name="memory-lease", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    @staticmethod
    def _interval() -> float:
        """续租间隔:取配置值与「租约的三分之一」里更小的那个(留出两次失败的机会)。"""
        lease = float(get_settings().memory_job_lease_seconds)
        want = float(get_settings().memory_job_renew_seconds)
        return max(0.02, min(want, max(0.02, lease / 3.0)))

    def _loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            if not self._renew():
                self.lost.set()
                return

    def _renew(self) -> bool:
        try:
            with db.session_scope() as session:
                ok = repo.renew_lease(session, job_id=self._job.id, owner=self._worker.owner,
                                      claim_token=self._job.claim_token, now=db.utc_naive(),
                                      lease_seconds=float(get_settings().memory_job_lease_seconds))
        except Exception as exc:  # noqa: BLE001 —— 数据库抖动:这一次没续上,下次再试
            logger.warning("记忆任务 %s 续租失败(%s),稍后再试", self._job.id[:8],
                           type(exc).__name__)
            return True
        if ok:
            self.renewals += 1
        else:
            logger.warning("记忆任务 %s 的租约已不属于本次认领,执行方停止提交", self._job.id[:8])
        return ok


class MemoryWorker:
    """后台提取线程(进程内;测试里可直接调 run_once)。

    `scope` 决定这个 worker 认领与维护哪一批数据:默认正式(`''`);评测脚本用
    `scope=eval_scope(run_id)` 起一个只认自己那一个 run 的 worker(见 scripts/memory_eval.py)。
    """

    def __init__(self, *, scope: str = SCOPE_FORMAL) -> None:
        self._scope = scope
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._run_lock = threading.Lock()          # 一轮只允许一个执行者
        self._db_backoff = 0.0
        self._db_retry_at = 0.0
        self._index_backoff = 0.0
        self._index_retry_at = 0.0
        self._drift_check_at = 0.0
        self._db_ok: bool | None = None
        self._db_error = ""
        self._last_error = ""
        self._last_run_at: str | None = None
        self._last_error_kind = ""
        # 租约身份只生成一次:认领与收尾必须是同一个 owner,否则收尾会被判成「租约不是你的」
        host = (socket.gethostname() or "host")[:40]
        self._owner = f"{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.claimed = 0
        self.succeeded = 0
        self.failed = 0
        self.recovered = 0
        self.indexed = 0
        self.ops = 0

    @property
    def scope(self) -> str:
        """这个 worker 认领的作用域(正式 = 空串;评测 = 'eval:<run-id>')。"""
        return self._scope

    # ---- 生命周期 ----

    def start(self) -> None:
        """启动后台线程(应用 lifespan 调用);未启用 / 未配库时什么都不做。"""
        if not get_settings().memory_worker_enabled or not db.enabled():
            return
        self._ensure_thread()

    def stop(self, *, timeout: float = 5.0) -> None:
        """关停:置停止位并等线程退出(正在跑的**一个**任务会跑完,不会中断在半路)。"""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            channel = "memory-worker" if not self._scope else f"memory-worker-{self._scope[:16]}"
            self._thread = threading.Thread(target=self._loop, name=channel, daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        interval = max(0.5, float(get_settings().memory_worker_interval_seconds))
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 —— 后台线程绝不因单轮异常退出
                logger.warning("记忆后台线程单轮异常(%s)", type(exc).__name__)
            if self._stop.wait(interval):
                break

    # ---- 一轮 ----

    def run_once(self, *, limit: int | None = None, force: bool = False) -> dict:
        """跑一轮:恢复 → 认领 → 执行 → 收尾(+ 清理台账、空闲时重放索引)。

        `force=True` 忽略数据库退避(运维手动触发 / 测试),不退避地真试一次。
        """
        if not force and self._db_retry_at and time.monotonic() < self._db_retry_at:
            return {"skipped": "数据库退避中"}
        with self._run_lock:
            try:
                return self._run_once(limit=limit, force=force)
            except MemoryNotConfigured:
                self._note_db_failure("未配置数据库或数据库不可用")
                raise
            except Exception as exc:  # noqa: BLE001 —— 拿不到任务:退避后再来,不抛给调用方
                self._note_db_failure(type(exc).__name__)
                logger.warning("记忆任务轮次失败(%s),稍后重试", type(exc).__name__)
                return {"error": type(exc).__name__}

    def _run_once(self, *, limit: int | None, force: bool = False) -> dict:
        stats = {"claimed": 0, "succeeded": 0, "failed": 0, "recovered": 0, "indexed": 0,
                 "ops": 0}
        if not get_settings().memory_write_enabled:
            # 暂停自动写入:一件任务都不领(队列原样留着,开回来继续)。
            # 但**清理台账与索引重放照跑**:那是已经发生过的删除与派生数据的收尾,
            # 既不产生新记忆、也不读对话内容 —— 用户的删除意图不该因为暂停写入而搁置。
            stats["ops"] = self._replay_ops(limit=None)
            stats["indexed"] = self._replay_index(force=force)
            return {**stats, "skipped": "自动写入已暂停(MEMORY_WRITE_ENABLED=false)"}
        if limit is None:
            limit = int(get_settings().memory_worker_tick_limit)
            limit = TICK_CAP if limit <= 0 else min(limit, TICK_CAP)
        now = db.utc_naive()
        with db.session_scope() as session:
            stats["recovered"] = repo.requeue_expired(session, now=now, scope=self._scope)
            stats["recovered"] += repo.requeue_expired_ops(session, now=now, scope=self._scope)
        if stats["recovered"]:
            self.recovered += stats["recovered"]
            logger.info("记忆任务:恢复了 %d 个租约过期的任务 / 清理操作", stats["recovered"])

        for _ in range(max(0, limit)):
            with db.session_scope() as session:               # 认领:短事务,立即提交
                job = repo.claim_job(session, owner=self.owner, now=db.utc_naive(),
                                     lease_seconds=float(get_settings().memory_job_lease_seconds),
                                     kinds=(JOB_KIND_EXTRACT,), scope=self._scope)
            if job is None:
                break
            stats["claimed"] += 1
            self.claimed += 1
            status = self._execute(job)
            stats[status] += 1
            if status == "succeeded":
                self.succeeded += 1
            else:
                self.failed += 1

        stats["ops"] = self._replay_ops(limit=None)
        if stats["claimed"] == 0:
            stats["indexed"] = self._replay_index(force=force)
            self.indexed += stats["indexed"]
        self._db_ok = True
        self._db_error = ""
        self._db_backoff = 0.0
        self._db_retry_at = 0.0
        self._last_run_at = _now_iso()
        return stats

    # ---- 一个任务 ----

    def _execute(self, job) -> str:
        """执行一个已认领的任务,返回 'succeeded' / 'failed'(收尾已经在库里写好)。"""
        stages = dict(job.stages or {})
        fence = service.Fence(job_id=job.id, owner=self.owner, claim_token=job.claim_token,
                              generation=int(job.generation or 0),
                              scope=job.scope or self._scope)
        payload = job.payload or {}
        messages = payload.get("messages") or []
        note = str(payload.get("extract_note") or "")
        # 上一轮存下来的提取结果还能不能用(输入摘要 + 协议 + 模型口径三项都要对上)
        report = self._reuse_extraction(stages, messages, note) if messages else None
        reused = report is not None
        if job.committed_at is not None:
            total = stages.get(STAGE_TOTAL)
            if (isinstance(total, int) and total >= 0
                    and set(_done_indices(stages)) == set(range(total))):
                return self._finalize_committed(job, fence)
            # 已有提交后固定原事实清单和下标；换模型不应触发重新提取。
            if report is None:
                from app.memory import extract

                try:
                    record = stages.get(STAGE_EXTRACT) or {}
                    report = extract.restore(record.get("result") or "")
                    if not isinstance(total, int) or len(report.facts) != total:
                        raise MemoryExtractionError("已提交任务的事实清单与阶段总数不匹配")
                    reused = True
                except MemoryExtractionError:
                    self._fail(job, fence, "部分事实已提交但原提取清单不可恢复，需人工处理",
                               permanent=True)
                    return "failed"
        if not messages:
            self._fail(job, fence, "任务里没有可提取的对话内容", permanent=True)
            return "failed"
        if not self._generation_ok(job):
            # 入队之后用户「彻底删除」过记忆:这个任务整体作废 —— 不花模型调用,
            # 也不会把已经删掉的事实写回来(落库时 repo 还会再校验一次)
            self._fail(job, fence, "用户已清除长期记忆,本任务作废", permanent=True)
            return "failed"

        lease = _LeaseGuard(self, job)
        lease.start()
        try:
            if report is None:
                # 真正调一次模型;拿到结果立刻把阶段写回任务行 —— 之后无论在哪一步挂掉,
                # 重试都不会再为这一步付费(见 _extract)
                report = self._extract(messages, note=note, stages=stages, job=job, fence=fence)
            if lease.lost.is_set():
                # 续租已判定「这份认领不成立」:后面的落库注定被 fencing 拦下,不如现在就停,
                # 不白花后面的调用
                self._fail(job, fence, "租约已不被本次认领持有,停止执行", permanent=False)
                return "failed"
            result = service.apply_facts(
                user_id=job.user_id, facts=report.facts, thread_id=job.thread_id,
                reason="会话提取", generation=job.generation, scope=fence.scope,
                turn_id=job.turn_id, fence=fence,
                # 已提交过的输入下标只在「复用了上一轮的提取结果」时才作数:换了提取结果,
                # 下标就对不上另一批事实了
                skip=frozenset(_done_indices(stages)) if reused else frozenset(),
                stages=stages)
        except MemoryExtractionError as exc:
            # 结构错误在提取 / 决策层已经重试过:同一份输入再跑一遍也还是不行,不再占用重试
            self._fail(job, fence, str(exc), permanent=True)
            return "failed"
        except MemoryNotConfigured as exc:
            self._fail(job, fence, str(exc), permanent=True)
            return "failed"
        except MemoryStaleGeneration:
            # 执行到一半用户「彻底删除」了记忆(落库时的代次校验拦下了这次写入):
            # 重跑一遍结果还是作废 —— 不重试、不花钱,如实记成「已作废」
            self._fail(job, fence, "用户已清除长期记忆,本任务作废", permanent=True)
            return "failed"
        except MemoryLeaseLost as exc:
            # 提交前的 fencing 校验拦下了这次写入(租约已被接管 / 已过期):本次什么都不能写,
            # 连失败收尾也不能 —— 任务已经归新的执行者,由它按已存下的阶段结果接着做
            self._note_lease_lost(job, exc)
            return "failed"
        except Exception as exc:  # noqa: BLE001 —— 网络 / 数据库抖动:退回 pending 退避重试
            self._fail(job, fence, f"{type(exc).__name__}", permanent=False, log_exc=True)
            return "failed"
        finally:
            lease.stop()

        _merge_done(stages, result.done)
        self._save_stages(job, fence, stages)              # 尽力而为:丢了只会多花一次调用
        if result.needs_retry:
            # 有事实没能生效(维护决策不合法 / 执行期冲突):**不标成功**。已提交的部分不回滚
            # (它们是有效租约下的既成事实),重试时按阶段结果跳过,只重做没生效的那几条。
            self._fail(job, fence, result.rejected or "部分事实未能生效", permanent=False)
            return "failed"
        if result.outcome.changed or result.per_fact:
            logger.info("记忆任务完成:新增 %d / 改写 %d / 删除 %d(丢弃 %d,降级 %d)",
                        len(result.outcome.added), len(result.outcome.updated),
                        len(result.outcome.deleted), len(result.outcome.dropped),
                        len(result.degraded))
        if result.index.deferred:
            self._note_index_failure(result.index.error)
        self._finish(job, fence, self._outcome_of(result))
        return "succeeded"

    # ---- 提取(阶段结果复用) ----

    def _reuse_extraction(self, stages: dict | None, messages: list,
                          note: str = "") -> object | None:
        """上一轮存的提取结果还能不能直接用(能就不再调用模型)。

        复用的三个条件缺一不可:同一份输入(摘要相同)、同一个协议版本、同一个模型口径。
        存下来的清单还要过**同一套**校验(见 extract.restore)—— 复用不等于免检。
        """
        from app.memory import extract

        record = (stages or {}).get(STAGE_EXTRACT)
        if not isinstance(record, dict):
            return None
        if (record.get("digest") != _messages_digest(messages, note)
                or record.get("protocol") != extract.PROTOCOL_VERSION
                or record.get("model") != service.llm_model_identity()):
            return None
        try:
            report = extract.restore(record.get("result") or "")
        except MemoryExtractionError as exc:
            logger.info("存下来的提取结果已不适用(%s),重新提取", type(exc).__name__)
            return None
        logger.info("复用上一轮已付费的提取结果(不再调用模型)")
        return report

    def _extract(self, messages, *, stages: dict, job, fence, note: str = ""):
        """调模型提取,并把阶段结果写回任务行(失败重试据此不再重复付费)。"""
        from app.memory import extract

        report = extract.extract_facts(messages, note=note)
        stages[STAGE_TOTAL] = len(report.facts)
        stages[STAGE_EXTRACT] = {
            "digest": _messages_digest(messages, note),
            "protocol": extract.PROTOCOL_VERSION,
            "model": service.llm_model_identity(),
            "status": "ok",
            "facts": len(report.facts),
            "dropped": len(report.dropped),
            "attempts": report.attempts,
            "result": extract.dump(report),               # 只含校验通过的事实
        }
        self._save_stages(job, fence, stages)
        return report

    def _finalize_committed(self, job, fence) -> str:
        """事实已提交、这次只收尾:补一轮索引后把任务标成功,不重新提取、不改事实。"""
        logger.warning("记忆任务 %s 的事实已提交:本次只收尾(不再调模型、不改事实)", job.id[:8])
        deferred = 0
        try:
            indexed = service.ensure_indexed(user_id=job.user_id,
                                             limit=int(get_settings().memory_worker_index_batch),
                                             scope=fence.scope)
            deferred = indexed.deferred
        except Exception as exc:  # noqa: BLE001 —— 索引是派生数据:留待重放,任务照样收尾
            deferred = 1
            self._note_index_failure(type(exc).__name__)
        self._finish(job, fence, {"finalized_only": True, "index_deferred": deferred})
        return "succeeded"

    def _save_stages(self, job, fence, stages: dict) -> bool:
        """把阶段结果写回任务行(短事务)。写不上只说明认领可能已被接管 —— 不抛。"""
        try:
            with db.session_scope() as session:
                ok = repo.save_stages(session, job_id=job.id, owner=self.owner,
                                      claim_token=fence.claim_token, stages=stages,
                                      now=db.utc_naive())
        except Exception as exc:  # noqa: BLE001 —— 丢了只是下次多花一次调用,不影响事实
            logger.warning("记忆任务 %s 阶段结果写入失败(%s)", job.id[:8], type(exc).__name__)
            return False
        if not ok:
            logger.warning("记忆任务 %s 写阶段结果时租约已不属于本次认领", job.id[:8])
        return ok

    def _generation_ok(self, job) -> bool:
        """任务入队时看到的代次还是当前代次吗(用户没在这期间清除过记忆)。"""
        try:
            with db.session_scope() as session:
                current = repo.generation_of(session, job.user_id, scope=job.scope)
        except Exception as exc:  # noqa: BLE001 —— 读不到就当不通过?不:那会把任务白白作废
            logger.warning("记忆任务 %s 代次校验失败(%s),本次照常执行(落库时还会再校验)",
                           job.id[:8], type(exc).__name__)
            return True
        return int(current) == int(job.generation or 0)

    @staticmethod
    def _outcome_of(result) -> dict:
        """执行结果摘要(只放计数与标记,不含正文;进任务行的 outcome 列)。"""
        return {
            "added": len(result.outcome.added), "updated": len(result.outcome.updated),
            "deleted": len(result.outcome.deleted), "skipped": result.outcome.skipped,
            "failed": len(result.pending), "index_deferred": result.index.deferred,
            "dropped": len(result.outcome.dropped), "degraded": len(result.degraded),
            "rejected": (result.rejected or "")[:200],     # 我们自己写的说明,不含正文
        }

    def _finish(self, job, fence, outcome: dict | None) -> None:
        with db.session_scope() as session:
            if not repo.finish_job(session, job_id=job.id, owner=self.owner,
                                   claim_token=fence.claim_token, now=db.utc_naive(),
                                   outcome=outcome):
                # 租约已经不归自己(过期后被别人重新认领):如实记一句,不假装成功
                logger.warning("记忆任务 %s 收尾时租约已不属于本进程,跳过", job.id[:8])

    def _note_lease_lost(self, job, exc: BaseException) -> None:
        """租约被接管:不写任何收尾(写也写不上),只如实留痕。"""
        logger.warning("记忆任务 %s 已被接管,本次不再提交(%s)", job.id[:8], type(exc).__name__)
        self._last_error = str(exc)
        self._last_error_kind = "lease_lost"

    def _fail(self, job, fence, error: str, *, permanent: bool, log_exc: bool = False) -> None:
        with db.session_scope() as session:
            status = repo.fail_job(session, job_id=job.id, owner=self.owner,
                                   claim_token=fence.claim_token, error=error,
                                   now=db.utc_naive(), permanent=permanent,
                                   backoff_seconds=float(get_settings().memory_job_backoff_seconds))
        if status is None:
            logger.warning("记忆任务 %s 失败收尾时租约已不属于本进程,跳过", job.id[:8])
        elif status == "failed":
            logger.warning("记忆任务 %s 失败:%s", job.id[:8], error)   # error 只含类名 / 我们自己的说明
        else:
            logger.info("记忆任务 %s 稍后重试:%s", job.id[:8], error)
        self._last_error = error
        self._last_error_kind = "permanent" if permanent else "retryable"
        if log_exc:                       # 外来异常:只记类名,不记消息(消息可能带 SQL 参数=正文)
            logger.warning("记忆任务执行遇到 %s,已退回重试队列", error)

    # ---- 清理台账(删除 / 彻底清除的向量收尾) ----

    def _replay_ops(self, *, limit: int | None = None) -> int:
        """把删除 / 彻底清除留下的向量清理台账做掉(幂等;失败按退避重试)。

        「先登记、再执行」的另一半:接口在事务外立刻试一次,成功就收尾;失败的那些
        由这里按退避接着跑 —— 否则残留向量只会随集合越积越多,而且没有任何地方会再试。
        """
        if limit is None:
            limit = max(1, int(get_settings().memory_worker_ops_batch))
        done = 0
        for _ in range(max(0, limit)):
            with db.session_scope() as session:
                op = repo.claim_op(session, owner=self.owner, now=db.utc_naive(),
                                   lease_seconds=float(get_settings().memory_job_lease_seconds),
                                   scope=self._scope)
            if op is None:
                break
            if self._run_op(op):
                done += 1
        self.ops += done
        return done

    def _run_op(self, op) -> bool:
        """执行一条清理台账;返回是否成功(失败已经按退避放回队列 / 落 failed)。"""
        try:
            if op.kind == OP_PURGE_USER:
                # 只删「代次小于新代次」的点:清除期间用户新写的记忆不受影响
                vector.delete_user(op.user_id, scope=op.scope,
                                   before_generation=int(op.generation))
            elif op.kind == OP_DELETE_VECTOR:
                vector.delete_ids([op.memory_id or ""], scope=op.scope)
            elif op.kind == OP_DELETE_OLD_VERSIONS:
                vector.delete_older_revisions(op.memory_id or "",
                                              revision=int(op.payload["revision"]), scope=op.scope)
            else:
                raise ValueError(f"未知的清理操作类型:{op.kind}")
        except Exception as exc:  # noqa: BLE001 —— 清理失败不是事实错误:退避重试
            self._last_error = type(exc).__name__
            self._last_error_kind = "cleanup"
            with db.session_scope() as session:
                status = repo.fail_op(
                    session, op_id=op.id, owner=self.owner, claim_token=op.claim_token,
                    error=type(exc).__name__, now=db.utc_naive(),
                    backoff_seconds=float(get_settings().memory_op_backoff_seconds))
            logger.warning("记忆清理台账 %s 未完成(%s),状态:%s", op.id[:8],
                           type(exc).__name__, status or "已被接管")
            return False
        with db.session_scope() as session:
            ok = repo.finish_op(session, op_id=op.id, owner=self.owner,
                                claim_token=op.claim_token, now=db.utc_naive())
        if ok:
            logger.info("记忆清理台账 %s 已完成(%s)", op.id[:8], op.kind)
        return ok

    # ---- 索引重放(自愈) ----

    def _replay_index(self, *, limit: int | None = None, force: bool = False) -> int:
        """把「索引与事实对不上」的有效记忆补进 Qdrant(空闲时才做)。

        `force=True`(运维手动触发 / 测试)不看退避:人工叫一次就真去试一次 ——
        退避是给后台自动轮次防打爆用的。
        """
        settings = get_settings()
        if not force and self._index_retry_at and time.monotonic() < self._index_retry_at:
            return 0
        self._drift_seen(force=force)
        try:
            result = service.ensure_indexed(limit=int(limit or settings.memory_worker_index_batch),
                                            scope=self._scope)
        except Exception as exc:  # noqa: BLE001 —— 索引是派生数据:失败只退避,不影响事实
            self._note_index_failure(type(exc).__name__)
            return 0
        if result.deferred:
            self._note_index_failure(result.error)
            return 0
        self._index_backoff = 0.0
        self._index_retry_at = 0.0
        if result.indexed or result.payload_only:
            logger.info("记忆索引重放:补写 %d 条(仅刷新 payload %d 条)",
                        result.indexed, result.payload_only)
        return result.indexed

    def _drift_seen(self, *, force: bool) -> bool:
        """周期核验当前版本点及 payload；发现缺失或不一致后修复标记。"""
        if not bool(get_settings().memory_worker_index_repair):
            return False
        now = time.monotonic()
        if not force and now < self._drift_check_at:
            return False
        self._drift_check_at = now + max(
            1.0, float(get_settings().memory_worker_drift_seconds))
        try:
            drift = service.index_drift(scope=self._scope, repair=True)
        except Exception as exc:  # noqa: BLE001 —— 对账失败不该影响补索引本身
            logger.warning("记忆索引对账失败(%s)", type(exc).__name__)
            return False
        if drift.get("drift"):
            logger.warning("记忆索引对账发现漂移(核验 %s 条，不一致 %s 条),已标记对应版本待修复",
                           drift.get("items"), drift.get("mismatched"))
            return True
        return False

    # ---- 状态 ----

    def _note_db_failure(self, why: str) -> None:
        self._db_ok = False
        self._db_error = why
        self._db_backoff = min(DB_BACKOFF_MAX,
                               DB_BACKOFF_START if self._db_backoff <= 0 else self._db_backoff * 2)
        self._db_retry_at = time.monotonic() + self._db_backoff

    def _note_index_failure(self, why: str) -> None:
        self._index_backoff = min(INDEX_BACKOFF_MAX, INDEX_BACKOFF_START if self._index_backoff <= 0
                                  else self._index_backoff * 2)
        self._index_retry_at = time.monotonic() + self._index_backoff
        logger.warning("记忆索引重放失败(%s),%d 秒后再试", why, int(self._index_backoff))

    @property
    def owner(self) -> str:
        """租约持有者标识:主机:进程:随机串 —— 重启后不会撞上上一轮的身份。"""
        return self._owner

    def status(self) -> dict:
        jobs: dict = {}
        ops: dict = {}
        db_ok, db_error = self._db_ok, self._db_error
        if db.enabled():
            try:
                with db.session_scope() as session:
                    jobs = repo.job_counts(session, scope=self._scope)
                    ops = repo.op_counts(session, scope=self._scope)
                if db_ok is None:
                    db_ok = True
            except Exception as exc:  # noqa: BLE001
                db_ok, db_error = False, type(exc).__name__
        return {
            "enabled": bool(get_settings().memory_worker_enabled),
            "configured": db.enabled(),
            "running": bool(self._thread and self._thread.is_alive()),
            "scope": self._scope,
            "db_ok": db_ok,
            "db_error": db_error,
            "jobs": jobs,
            "ops": ops,
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "recovered": self.recovered,
            "indexed": self.indexed,
            "ops_replayed": self.ops,
            "last_run_at": self._last_run_at,
            "last_error": self._last_error,
            "last_error_kind": self._last_error_kind,
        }


_worker: MemoryWorker | None = None
_worker_lock = threading.Lock()


def get_worker() -> MemoryWorker:
    global _worker
    if _worker is None:
        with _worker_lock:
            if _worker is None:
                _worker = MemoryWorker()
    return _worker


def install_worker(worker: MemoryWorker | None) -> None:
    """测试用:替换单例(None = 下次按配置重建)。"""
    global _worker
    _worker = worker


def start() -> None:
    get_worker().start()


def stop(*, timeout: float = 5.0) -> None:
    get_worker().stop(timeout=timeout)


def status() -> dict:
    return get_worker().status()
