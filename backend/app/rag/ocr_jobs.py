"""索引任务:持久化状态 + 单工作者执行 + 重启对账(本地 JSON,不引入数据库)。

目录(`{INDEX_STATE_DIR}/jobs/`):
  <job_id>.json         任务状态与进度(每处理完一个文件原子重写一次)
  <job_id>.files.jsonl  文件明细(追加写,分页读取)
  worker.lock           单工作者锁(记录 job_id / pid / 心跳)

并发模型:接口进程里起一个后台线程跑任务(FastAPI 的同步路由本来就在线程池里),
同一时刻只允许一个任务 —— 由 worker.lock 保证。生产是 uvicorn `--workers 2`,两个进程
共享同一份锁文件,所以锁必须跨进程有效:采用「原子创建 + 心跳」,不依赖进程存活检测
(Windows 上 os.kill(pid, 0) 会直接杀进程,不能用)。

重启对账:进程启动时把所有还停在 queued / running 的任务标记为中断失败 —— 任务不会
自己复活,但已识别页面都在 OCR 缓存里,重新排队几乎不会重复付费。锁心跳超过
LOCK_STALE_SECONDS 视为陈旧,可被新任务接管(原先的任务由对账标记失败)。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.metering.context import PURPOSE_INDEX, bind as bind_context
from app.rag import documents, ingest, oss, store
from app.rag.ingest import Options, Scope
from app.rag.localfs import atomic_write_json, read_json

logger = logging.getLogger(__name__)

LOCK_STALE_SECONDS = 30 * 60    # 心跳超过这个时长才算陈旧(单文件最长耗时远小于它)
JOB_KEEP = 50                   # 任务文件保留个数
_RUNNING = ("queued", "running")


class JobBusy(RuntimeError):
    """已有任务在跑:接口层转 409,避免两个任务同时发布互相覆盖。"""

    def __init__(self, owner: dict | None) -> None:
        owner = owner or {}
        super().__init__(f"已有索引任务在运行:{owner.get('job_id', '未知')}")
        self.owner = owner


class JobSuperseded(RuntimeError):
    """本任务的工作者锁已被别的任务接管(心跳过期),应立即停手。"""


def jobs_dir() -> Path:
    return Path(get_settings().index_state_dir) / "jobs"


def job_path(job_id: str) -> Path:
    return jobs_dir() / f"{job_id}.json"


def files_path(job_id: str) -> Path:
    return jobs_dir() / f"{job_id}.files.jsonl"


def _lock_path() -> Path:
    return jobs_dir() / "worker.lock"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- 工作者锁 ----

def lock_owner() -> dict | None:
    """锁归属信息;无锁返回 None。"""
    data = read_json(_lock_path(), default=None)
    return data if isinstance(data, dict) else None


def _is_fresh(lock: dict) -> bool:
    try:
        age = datetime.now(timezone.utc).timestamp() - float(lock.get("heartbeat") or 0)
    except Exception:
        return False
    return age < LOCK_STALE_SECONDS


def job_running() -> dict | None:
    """有真在跑的任务时返回它的锁信息,否则 None(陈旧锁不算)。"""
    lock = lock_owner()
    return lock if lock and _is_fresh(lock) else None


def _take_lock(job_id: str) -> tuple[bool, dict | None]:
    """尝试占锁。返回 (是否拿到, 现有归属)。陈旧锁可接管,新鲜锁一律拒绝。"""
    lock = lock_owner()
    if lock and lock.get("job_id") != job_id and _is_fresh(lock):
        return False, lock
    atomic_write_json(_lock_path(), {
        "job_id": job_id, "pid": os.getpid(), "started_at": _now(),
        "heartbeat": datetime.now(timezone.utc).timestamp(),
    })
    return True, None


def touch_lock(job_id: str) -> None:
    """刷新心跳(每个文件刷一次);锁已被别人接管则抛 JobSuperseded。"""
    lock = lock_owner()
    if lock and lock.get("job_id") != job_id:
        raise JobSuperseded(f"锁已被 {lock.get('job_id')} 接管")
    atomic_write_json(_lock_path(), {
        "job_id": job_id, "pid": os.getpid(), "started_at": (lock or {}).get("started_at") or _now(),
        "heartbeat": datetime.now(timezone.utc).timestamp(),
    })


def release_lock(job_id: str) -> None:
    """释放锁(只释放自己的);任务结束后调用。"""
    lock = lock_owner()
    if lock and lock.get("job_id") == job_id:
        try:
            _lock_path().unlink()
        except OSError as exc:
            logger.warning("释放工作者锁失败(留待心跳过期):%s", exc)


# ---- 任务读写 ----

def new_job(scope: Scope, options: Options) -> dict:
    """建任务并占锁;已有任务在跑抛 JobBusy(接口转 409)。"""
    job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    ok, owner = _take_lock(job_id)
    if not ok:
        raise JobBusy(owner)
    job = {
        "job_id": job_id,
        "status": "queued",
        "scope": scope.as_dict(),
        "options": options.as_dict(),
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "progress": {"done": 0, "total": 0, "current": None},
        "published": None,
        "error": None,
        "summary": None,
    }
    _write(job)
    return job


def get_job(job_id: str) -> dict | None:
    data = read_json(job_path(job_id), default=None)
    return data if isinstance(data, dict) else None


def list_jobs(limit: int = 20) -> list[dict]:
    """最近的任务(新的在前),只读状态,不含文件明细。"""
    d = jobs_dir()
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[: max(1, limit)]
    out = []
    for p in files:
        data = read_json(p, default=None)
        if isinstance(data, dict):
            out.append(data)
    return out


def files_page(job_id: str, offset: int = 0, limit: int = 100) -> dict:
    """文件明细分页读取(明细在 JSONL 里,不在任务 JSON 里)。"""
    path = files_path(job_id)
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    offset = max(0, offset)
    limit = max(1, min(limit, 500))
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows[offset: offset + limit]}


def _write(job: dict) -> None:
    atomic_write_json(job_path(job["job_id"]), job)


def _append_file_detail(job_id: str, detail: dict) -> None:
    """追加一行文件明细;追加写单个小 JSON,不需要原子替换。"""
    path = files_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(detail, ensure_ascii=False) + "\n")


def _prune() -> None:
    """只保留最近 JOB_KEEP 个任务(json + jsonl),避免目录无限增长。"""
    d = jobs_dir()
    if not d.is_dir():
        return
    jobs = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in jobs[JOB_KEEP:]:
        for q in (p, p.with_suffix(".files.jsonl")):
            try:
                q.unlink()
            except OSError:
                pass
        documents.drop_publish_record(p.stem)   # 已执行完的缓存清单记录随之清理


# ---- 执行 ----

def start_job(job: dict) -> None:
    """起后台线程执行任务(路由立即返回 202,前端轮询进度)。"""
    t = threading.Thread(
        target=_run, args=(job["job_id"], job["scope"], job["options"]),
        name=f"index-job-{job['job_id']}", daemon=True,
    )
    t.start()


def _run(job_id: str, scope_dict: dict, options_dict: dict) -> None:
    job = get_job(job_id) or {"job_id": job_id}
    job.update({"status": "running", "started_at": _now()})
    _write(job)
    scope = Scope(kind=scope_dict.get("kind", "prefix"),
                  prefix=scope_dict.get("prefix", "") or "",
                  keys=tuple(scope_dict.get("keys") or ()))
    opts = Options(extraction_mode=options_dict.get("extraction_mode", "native_only"),
                   mixed_invoice=bool(options_dict.get("mixed_invoice")),
                   refresh_ocr=bool(options_dict.get("refresh_ocr")))
    try:
        def progress(done: int, total: int, detail: dict) -> None:
            touch_lock(job_id)               # 心跳:锁被接管会抛 JobSuperseded,立刻停手
            _append_file_detail(job_id, detail)
            job["progress"] = {"done": done, "total": total, "current": detail.get("key")}
            _write(job)

        # 调用日志归属:本任务线程内所有 OCR / 向量化调用都记在这个任务名下(§3)
        with bind_context(purpose=PURPOSE_INDEX, job_id=job_id):
            summary = ingest.run_index_job(oss.knowledge_store(), scope, opts, progress=progress,
                                           job_id=job_id)
        files = summary.pop("files", [])
        for d in files[_read_details_count(job_id):]:   # 兜底:回调漏记的明细补上
            _append_file_detail(job_id, d)
        job.update({
            "status": "published" if summary.get("published") else "failed",
            "finished_at": _now(),
            "published": bool(summary.get("published")),
            "summary": summary,
            "error": None if summary.get("published") else (summary.get("message") or "任务未发布"),
        })
        logger.info("索引任务 %s 结束:%s", job_id, job["status"])
    except Exception as exc:   # 依赖未配置 / 锁被接管 / 其它异常:如实记录,锁在 finally 释放
        logger.exception("索引任务 %s 失败", job_id)
        job.update({"status": "failed", "finished_at": _now(), "published": False,
                    "error": f"{type(exc).__name__}:{exc}"})
    finally:
        _write(job)
        release_lock(job_id)
        _prune()


def _read_details_count(job_id: str) -> int:
    try:
        with files_path(job_id).open("r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except FileNotFoundError:
        return 0


# ---- 重启对账 ----

def reconcile() -> int:
    """启动时对账:所有还停在 queued / running 的任务标记为中断失败;清掉陈旧锁。

    顺带恢复两件只差收尾的事(都不重跑 OCR):发布成功但缓存清单没更新的记录、以及
    文件删除后的缓存清理。返回受影响的任务数。中断不等于白干:已识别页面都在缓存里,
    重新排队时命中两层 OCR 缓存。
    """
    n = 0
    d = jobs_dir()
    if d.is_dir():
        for p in d.glob("*.json"):
            data = read_json(p, default=None)
            if not isinstance(data, dict) or data.get("status") not in _RUNNING:
                continue
            data.update({"status": "failed", "finished_at": _now(), "published": False,
                         "error": "服务重启中断,可重新排队(已识别页面保留在缓存里,不会重复计费)"})
            atomic_write_json(p, data)
            n += 1
    # 锁指向的任务既然已经失败/不存在,锁就是死锁,清掉 —— 否则重启后新建任务会被
    # 一把"心跳还新鲜"的旧锁挡住,直到它超时(30 分钟)才可用。
    lock = lock_owner()
    if lock:
        j = get_job(str(lock.get("job_id")))
        if j is None or j.get("status") not in _RUNNING:
            release_lock(str(lock.get("job_id") or ""))
    if n:
        logger.warning("启动对账:%d 个索引任务被标记为中断失败", n)

    try:                       # 缓存 / Redis 不可用时只告警:对账失败不该拖垮启动
        active = store.active_collection()
        if documents.retry_pending_publishes(active):
            logger.warning("启动对账:补完了未收尾的缓存清单更新")
        if documents.retry_pending_deletions():
            logger.warning("启动对账:补完了未收尾的文件缓存清理")
    except Exception as exc:   # noqa: BLE001 —— Qdrant / Redis 未接通都算,记录后继续启动
        logger.warning("启动对账:缓存清单 / 删除清理重试跳过(%s)", exc)
    return n


def current_job() -> dict | None:
    """正在跑的任务(有锁且未陈旧);没有则返回最近一个任务。"""
    lock = lock_owner()
    if lock and _is_fresh(lock):
        j = get_job(str(lock.get("job_id")))
        if j:
            return j
    jobs = list_jobs(1)
    return jobs[0] if jobs else None
