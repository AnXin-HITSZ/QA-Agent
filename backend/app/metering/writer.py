"""调用日志写入器:进程内队列 + 后台补写线程 + 本地故障恢复目录(技术方案 §8)。

为什么不让业务线程直接写库:
- OCR / 向量化在索引线程或请求线程里跑,一次 INSERT 的网络往返不该拖慢它们,更不该
  因为 MySQL 抖动让「已经成功的付费调用」在业务上变成失败并重试;
- 写库失败必须有去处:先落本地补写目录(持久、跨重启),等数据库恢复再补;
- 只有「数据库提交成功」才删除补写文件;重放靠 event_id 主键幂等,不会重复计数。

线程模型:每个进程一个后台线程(懒启动、守护线程),业务线程只做入队(O(1))。
数据库不可用时线程按退避重试,不阻塞启动、不阻塞请求。队列满时直接落盘,不丢。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from app.config import get_settings
from app.metering import db, pending, pricing
from app.metering.model import (
    COST_UNKNOWN, NOTE_PRICE_NOT_LOADED, CallEvent, CacheEvent, payload_kind,
)
from app.metering.store import CallFilter, get_store

logger = logging.getLogger(__name__)

ALLOCATION_NOTE = "按文本数比例分摊(估算,非账单金额)"
ALLOCATION_NOTE_SINGLE = "该请求仅归属此文件(金额为估算)"
FLUSH_MIN_INTERVAL = 0.5          # 后台线程每轮之间至少间隔,避免空转打满 CPU
BACKOFF_START = 5.0
BACKOFF_MAX = 300.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _split(payloads: list[dict]) -> tuple[list[CallEvent], list[CacheEvent]]:
    calls, caches = [], []
    for p in payloads:
        kind = payload_kind(p)
        if kind == "call":
            calls.append(CallEvent.from_payload(p))
        elif kind == "cache":
            caches.append(CacheEvent.from_payload(p))
    return calls, caches


def allocate_items(event: CallEvent) -> CallEvent:
    """把事件金额按文本数比例分摊到各文件(items),标注为估算;无金额则只留归属。"""
    if not event.items:
        return event
    total = sum(max(0, int(i.text_count)) for i in event.items)
    out = []
    for item in event.items:
        if event.cost_amount is None or total <= 0:
            out.append(item.with_(allocated_cost=None,
                                  allocation_note="无金额可分摊(费用未估算)"))
            continue
        share = (Decimal(max(0, int(item.text_count))) / Decimal(total)).quantize(
            Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        out.append(item.with_(
            allocated_cost=(event.cost_amount * share).quantize(Decimal("0.00000001"),
                                                                rounding=ROUND_HALF_UP),
            allocation_note=ALLOCATION_NOTE_SINGLE if len(event.items) == 1 else ALLOCATION_NOTE,
        ))
    return event.with_(items=tuple(out))


class MeteringWriter:
    """调用日志的唯一写入口(业务线程调用 call/cache,其余全在后台线程)。"""

    def __init__(self) -> None:
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=max(100, int(get_settings().metering_queue_max)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._flush_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._backoff = 0.0
        self._retry_at = 0.0
        self._db_ok: bool | None = None
        self._db_error = ""
        self._last_flush_at: str | None = None
        self._last_error = ""
        self._flushed = 0
        self._backfilled = 0
        self._spilled = 0          # 入队失败 / 写库失败而落盘的事件数
        self._lost = 0             # 连补写文件也写不下的(只可能在磁盘故障时发生)
        # 延迟取 store:测试或运行期替换存储实现后,价格表跟着换源
        self._prices = pricing.PriceBook(lambda: get_store().price_rules(),
                                         ttl=float(get_settings().metering_price_cache_seconds))

    # ---- 生命周期 ----

    @property
    def price_book(self) -> pricing.PriceBook:
        return self._prices

    def start(self) -> None:
        """启动后台线程(应用 lifespan 调用;未配置数据库时不做任何事)。"""
        if not db.configured():
            return
        self._ensure_thread()

    def stop(self, *, timeout: float = 5.0) -> None:
        """关停:停线程、尽力冲一次队列(冲不掉的落盘,不留内存里)。"""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
        try:
            self.flush(timeout=timeout)
        except Exception:  # noqa: BLE001 —— 关停阶段不再抛
            logger.warning("关停时冲刷调用日志失败(记录已落补写目录)", exc_info=True)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="metering-flush", daemon=True)
            self._thread.start()

    # ---- 入队(业务线程调用;绝不抛异常、绝不阻塞在网络 IO 上) ----

    def call(self, event: CallEvent) -> None:
        self._put(event.as_payload())

    def cache(self, event: CacheEvent) -> None:
        self._put(event.as_payload())

    def _put(self, payload: dict) -> None:
        try:
            self._ensure_thread()
            self._queue.put_nowait(payload)
        except queue.Full:
            self._spill(payload, "队列已满")
        except Exception:  # noqa: BLE001 —— 任何意外都不该影响业务
            logger.warning("调用日志入队失败", exc_info=True)

    def _spill(self, payload: dict, why: str) -> None:
        """入队失败 / 写库失败 → 落本地补写目录;连落盘都失败就告警计数(不静默丢)。"""
        path = pending.write(payload)
        if path is None:
            self._lost += 1
            logger.error("调用日志补写失败(%s),事件 %s 未能保存", why, payload.get("event_id"))
            return
        self._spilled += 1

    # ---- 后台循环 ----

    def _loop(self) -> None:
        interval = max(0.2, float(get_settings().metering_flush_interval_seconds))
        last_ping = 0.0
        while not self._stop.wait(interval):
            try:
                self._flush_queue()
                self._backfill()
                now = time.monotonic()
                if now - last_ping > 15.0:
                    self._db_ok, self._db_error = db.ping()
                    last_ping = now
                    if self._db_ok:
                        self._prices.rules(refresh=True)   # 数据库回来了:顺便刷新价格表
            except Exception:  # noqa: BLE001 —— 后台线程绝不因单轮异常退出
                logger.exception("调用日志后台线程单轮异常")

    def _drain(self, limit: int) -> list[dict]:
        out: list[dict] = []
        for _ in range(max(1, limit)):
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out

    def flush(self, *, timeout: float | None = None, limit: int | None = None) -> dict:
        """同步冲刷(测试 / 关停用):队列与补写目录各走一轮。返回本轮统计。"""
        if timeout is not None:
            self._flush_lock.acquire(timeout=timeout)
        else:
            self._flush_lock.acquire()
        try:
            wrote = self._flush_queue(limit=limit)
            # 显式冲刷(运维手动 / 关停)不看退避:退避是给后台自动轮次防打爆用的,
            # 人工叫一次就该真去试一次。
            back = self._backfill(limit=limit, force=True)
            return {"written": wrote, "backfilled": back}
        finally:
            self._flush_lock.release()

    # ---- 队列 → 数据库 ----

    def _flush_queue(self, *, limit: int | None = None) -> int:
        batch = max(1, int(limit or get_settings().metering_flush_batch))
        payloads = self._drain(batch)
        if not payloads:
            return 0
        prepared = [self._prepare(p) for p in payloads]
        calls, caches = _split(prepared)
        try:
            inserted = get_store().insert(calls, caches)
        except Exception as exc:  # noqa: BLE001 —— 数据库不可用:全部落盘,稍后补写
            self._db_ok = False
            self._db_error = f"{type(exc).__name__}: {exc}"
            self._last_error = self._db_error
            self._bump_backoff()
            for p in prepared:
                self._spill(p, "写库失败")
            logger.warning("调用日志写库失败,已转入补写目录(%d 条):%s", len(prepared), exc)
            return 0
        self._db_ok = True
        self._db_error = ""
        self._last_error = ""
        self._backoff = 0.0
        self._retry_at = 0.0
        # 计数按「真正新插入的行数」:重复事件(理论上同一条进了两次队列)不算
        self._flushed += max(0, int(inserted))
        self._last_flush_at = _now_iso()
        return max(0, int(inserted))

    def _prepare(self, payload: dict) -> dict:
        """写库前补齐:价格(若记录时没载入)→ 分摊到各文件。"""
        if payload_kind(payload) != "call":
            return payload
        event = CallEvent.from_payload(payload)
        if event.cost_status == COST_UNKNOWN and event.cost_note == NOTE_PRICE_NOT_LOADED:
            event = self._requote(event)
        event = allocate_items(event)
        return event.as_payload()

    def _requote(self, event: CallEvent) -> CallEvent:
        """补价:仍按**事件发生时刻**选生效规则,不吃之后改价的影响。

        只在价格表**成功载入**时改写:载入失败(库不可用)保持「价格表未载入」原样,
        下一轮再补;载入成功但没配对应规则 / 缺用量时,把原因如实改成「未配置价格」
        —— 「没配」是需要运维去配置的,与「还没读到」的处置完全不同,不能一直显示前者。
        金额仍可能是 NULL:绝不为凑一个数字而按别的规则估算。
        """
        quote = self._prices.quote(
            service=event.service, provider=event.provider, target=event.target,
            unit=event.billing_unit or _default_unit(event.service),
            at=event.occurred_at, usage=event.usage_quantity,
        )
        if not self._prices.loaded:
            return event
        return event.with_(
            cost_amount=quote.cost, currency=quote.currency, cost_status=quote.status,
            cost_note=quote.note, price_id=quote.price_id, price_version=quote.price_version,
            price_snapshot=quote.snapshot, billing_quantity=quote.billing_quantity or event.billing_quantity,
            billing_unit=quote.billing_unit,
        )

    # ---- 补写目录 → 数据库 ----

    def _bump_backoff(self) -> None:
        self._backoff = min(BACKOFF_MAX, BACKOFF_START if self._backoff <= 0 else self._backoff * 2)
        self._retry_at = time.monotonic() + self._backoff

    def _backfill(self, *, limit: int | None = None, force: bool = False) -> int:
        if not force and self._retry_at and time.monotonic() < self._retry_at:
            return 0                                    # 退避中:不反复敲打故障中的数据库
        pending.sweep_stale_claims()
        batch = max(1, int(limit or get_settings().metering_flush_batch))
        files = pending.pending_files(limit=batch)
        if not files:
            return 0
        payloads: list[dict] = []
        claimed: list = []
        for path in files:
            got = pending.claim(path)                   # 原子领取:多 worker 只有一个成功
            if got is None:
                continue
            data = pending.read_payload(got)
            if data is None:                            # 损坏文件:放回并告警,不静默删
                logger.warning("补写文件无法解析,保留待人工处理:%s", got.name)
                pending.release(got)
                continue
            payloads.append(data)
            claimed.append(got)
        if not payloads:
            return 0
        prepared = [self._prepare(p) for p in payloads]
        calls, caches = _split(prepared)
        try:
            inserted = get_store().insert(calls, caches)
        except Exception as exc:  # noqa: BLE001
            self._db_ok = False
            self._db_error = f"{type(exc).__name__}: {exc}"
            self._last_error = self._db_error
            self._bump_backoff()
            for got in claimed:                          # 一条没写成功:全部放回
                pending.release(got)
            logger.warning("补写失败(%d 条),稍后重试:%s", len(claimed), exc)
            return 0
        for got in claimed:                              # 提交成功后才删除
            pending.finish(got)
        self._db_ok = True
        self._db_error = ""
        self._backoff = 0.0
        self._retry_at = 0.0
        # 与冲刷同口径:计新插入的行数;重放一条已入库的记录时这个数不会涨
        self._backfilled += max(0, int(inserted))
        self._last_flush_at = _now_iso()
        return max(0, int(inserted))

    # ---- 状态 ----

    def status(self) -> dict:
        counts = pending.count()
        return {
            "enabled": True,
            "configured": db.configured(),
            "running": bool(self._thread and self._thread.is_alive()),
            "db_ok": self._db_ok,
            "db_error": self._db_error,
            "queued": self._queue.qsize(),
            "pending": counts["pending"],
            "claimed": counts["claimed"],
            "pending_dir": counts["dir"],
            "flushed": self._flushed,
            "backfilled": self._backfilled,
            "spilled": self._spilled,
            "lost": self._lost,
            "last_flush_at": self._last_flush_at,
            "last_error": self._last_error,
            "price_rules": len(self._prices.rules()) if self._prices.loaded else 0,
            "price_error": self._prices.error,
        }


def _default_unit(service: str) -> str:
    return "1k_tokens" if service == "embedding" else "request"


_writer: MeteringWriter | None = None
_writer_lock = threading.Lock()


def get_writer() -> MeteringWriter:
    global _writer
    if _writer is None:
        with _writer_lock:
            if _writer is None:
                _writer = MeteringWriter()
    return _writer


def install_writer(writer: MeteringWriter | None) -> None:
    """测试用:替换单例(None = 下次按配置重建)。"""
    global _writer
    _writer = writer


def summary_filter(**kwargs) -> CallFilter:
    return CallFilter(**kwargs)
