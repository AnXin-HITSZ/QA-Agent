"""调用日志与费用统计:对外入口(业务代码只碰这个模块)。

用法(业务侧):
    from app.metering import metering
    metering.record_call(...)   # 或 metering.build_call(...) 构造后再提交
    metering.record_cache(...)
    metering.active()           # 便宜的开关判断,关闭时整条链路只做一次布尔判断

三条硬约束:
- **绝不抛异常**:计量失败不能影响业务(OCR / 索引 / 检索该成功就成功);
- **绝不阻塞**:入队是内存操作,写库 / 补写都在后台线程;
- **绝不重做付费调用**:补写只重放「记录」,永远不会重新发起 OCR / Embedding 请求。

统计口径(与前端 / 技术方案一致):
- 事件 = 一次真实外部请求;缓存命中单独记,不产生费用事件;
- 金额是「按配置单价估算」,不是官方账单;缺用量或缺价格时如实记 unknown,不记 0。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from app.metering import db, pending, pricing
from app.metering.context import CallContext, current as current_context, use as use_context
from app.metering.context import bind as bind_context
from app.metering.model import (
    BILLING_BILLABLE, BILLING_UNKNOWN, COST_ESTIMATED, COST_UNKNOWN, LAYER_EMBEDDING,
    LAYER_OCR_RAW, LAYER_OCR_TEXT, NOTE_PRICE_NOT_CONFIGURED, NOTE_PRICE_NOT_LOADED,
    SERVICE_EMBEDDING, SERVICE_LLM, SERVICE_OCR, SERVICE_RERANK, STATUS_FAILURE, STATUS_SUCCESS,
    USAGE_SOURCE_LOCAL_COUNT, USAGE_SOURCE_UNKNOWN, USAGE_SOURCE_VENDOR, CallEvent, CallItem,
    CacheEvent, utcnow,
)
from app.metering.pricing import Quote
from app.metering.redact import safe_endpoint, safe_text
from app.metering.store import CallFilter, get_store, install as install_store
from app.metering.writer import MeteringWriter, allocate_items, get_writer, install_writer

logger = logging.getLogger(__name__)

__all__ = [
    "CallContext", "CallEvent", "CallItem", "CallFilter", "CacheEvent", "MeteringWriter",
    "Quote", "active", "bind_context", "build_call", "current_context", "db", "get_store",
    "get_writer", "health", "install_store", "install_writer", "pending", "pricing",
    "record_cache", "record_cache_stats", "record_call", "start", "status", "stop",
    "use_context", "METERING_DISABLED_MESSAGE",
    "LAYER_EMBEDDING", "LAYER_OCR_RAW", "LAYER_OCR_TEXT",
    "SERVICE_EMBEDDING", "SERVICE_OCR", "SERVICE_LLM", "SERVICE_RERANK",
    "USAGE_SOURCE_VENDOR", "USAGE_SOURCE_LOCAL_COUNT", "USAGE_SOURCE_UNKNOWN",
]

METERING_DISABLED_MESSAGE = (
    "调用日志与费用统计未启用:请在 backend/.env 配置 MYSQL_URL"
    "(见 docs/调用日志与费用统计技术方案.md §9);未配置时不影响索引与检索。"
)


def new_id() -> str:
    return uuid.uuid4().hex


# ---- 开关 ----

def active() -> bool:
    """是否启用计量:配置了数据库地址且未显式关闭。业务侧用它做最便宜的前置判断。"""
    from app.config import get_settings

    try:
        return bool(get_settings().metering_enabled) and db.configured()
    except Exception:  # noqa: BLE001 —— 配置异常也当关闭,不影响业务
        return False


def start() -> None:
    """应用启动时调用(不阻塞:后台线程负责首次连接与价格表载入)。"""
    if not active():
        return
    get_writer().start()


def stop() -> None:
    try:
        get_writer().stop()
    except Exception:  # noqa: BLE001
        logger.warning("停止调用日志写入器失败", exc_info=True)
    try:
        db.dispose()
    except Exception:  # noqa: BLE001
        logger.warning("释放 MySQL 连接池失败", exc_info=True)


# ---- 计价 ----

def quote_for(*, service: str, provider: str, target: str, unit: str,
              usage: Decimal | None, at: datetime | None = None) -> Quote:
    """用内存里的价格表报价(不在业务线程里读库;未载入时如实记「价格表未载入」)。"""
    try:
        return get_writer().price_book.quote(service=service, provider=provider, target=target,
                                             unit=unit, at=at or utcnow(), usage=usage,
                                             allow_load=False)
    except Exception:  # noqa: BLE001 —— 报价失败按「无法估算」处理
        logger.warning("价格估算失败(按无法估算处理)", exc_info=True)
        return Quote(None, None, "", None, unit, COST_UNKNOWN, NOTE_PRICE_NOT_CONFIGURED)


def build_call(*, service: str, provider: str, target: str, endpoint: str, duration_ms: int,
               status: str, usage_quantity: Decimal | None = None, usage_unit: str = "",
               usage_source: str = USAGE_SOURCE_UNKNOWN, usage_note: str = "",
               billing_status: str = BILLING_UNKNOWN, billing_note: str = "",
               price_unit: str = "", error_class: str = "", error_message: str = "",
               http_status: int | None = None, provider_request_id: str | None = None,
               call_group: str = "", attempt_no: int = 1, retry_of: str | None = None,
               http_attempts: int = 1, items: Iterable[CallItem] = (),
               occurred_at: datetime | None = None, ctx: CallContext | None = None) -> CallEvent:
    """按当前上下文 + 价格表构造一条调用事件(不落库;落库由 record_call 负责)。

    这是对外唯一的事件构造入口,所以**脱敏在这里兜底**:错误摘要与端点无论上层传什么
    (哪怕整段异常字符串 / 带签名的完整 URL)都过一道闸再进事件,入库前不可能漏出密钥。
    调用方仍应尽量只传必要信息 —— 这里只是最后一道防线,不是第一道。
    """
    ctx = ctx or current_context()
    at = occurred_at or utcnow()
    unit = price_unit or ("1k_tokens" if service == SERVICE_EMBEDDING else "request")
    quote = quote_for(service=service, provider=provider, target=target, unit=unit,
                      usage=usage_quantity, at=at)
    return CallEvent(
        event_id=new_id(), service=service, purpose=ctx.purpose, provider=provider, target=target,
        occurred_at=at, duration_ms=max(0, int(duration_ms)), status=status,
        error_class=safe_text(error_class, limit=64), error_message=safe_text(error_message),
        http_status=http_status,
        provider_request_id=provider_request_id, endpoint=safe_endpoint(endpoint),
        call_group=call_group, attempt_no=attempt_no, retry_of=retry_of,
        http_attempts=max(1, int(http_attempts)),
        usage_quantity=usage_quantity, usage_unit=usage_unit, usage_source=usage_source,
        usage_note=usage_note, billing_status=billing_status, billing_note=billing_note,
        billing_quantity=quote.billing_quantity, billing_unit=quote.billing_unit or unit,
        cost_amount=quote.cost, currency=quote.currency, cost_status=quote.status,
        cost_note=quote.note,
        price_id=quote.price_id, price_version=quote.price_version, price_snapshot=quote.snapshot,
        job_id=ctx.job_id, document_id=ctx.document_id, oss_key=ctx.oss_key, page_no=ctx.page_no,
        items=tuple(items),
    )


# ---- 提交(绝不抛、绝不阻塞业务) ----

def record_call(event: CallEvent) -> None:
    if not active():
        return
    try:
        get_writer().call(event)
    except Exception:  # noqa: BLE001 —— 计量失败不影响业务
        logger.warning("调用事件提交失败(不影响业务)", exc_info=True)


def record_cache(event: CacheEvent) -> None:
    if not active():
        return
    try:
        get_writer().cache(event)
    except Exception:  # noqa: BLE001
        logger.warning("缓存事件提交失败(不影响业务)", exc_info=True)


def record_cache_stats(*, layer: str, unit: str, hit: int = 0, miss: int = 0, shared: int = 0,
                       skipped: int = 0, note: str = "") -> None:
    """缓存统计的便捷入口(由 ocr_cache / embedding_cache 调用)。"""
    if not active():
        return
    if hit <= 0 and miss <= 0 and shared <= 0 and skipped <= 0:
        return
    ctx = current_context()
    record_cache(CacheEvent(
        event_id=new_id(), layer=layer, purpose=ctx.purpose, unit=unit, hit_count=hit,
        miss_count=miss, shared_count=shared, skipped_count=skipped, occurred_at=utcnow(),
        job_id=ctx.job_id, document_id=ctx.document_id, oss_key=ctx.oss_key,
        page_no=ctx.page_no, note=note,
    ))


# ---- 状态 ----

def status() -> dict:
    """持久化健康(供 /admin/metering/summary 与前端「日志完整性」展示)。"""
    if not active():
        return {"enabled": False, "configured": db.configured(), "running": False,
                "queued": 0, "pending": 0, "claimed": 0, "lost": 0, "spilled": 0,
                "flushed": 0, "backfilled": 0, "db_ok": None, "db_error": "",
                "last_flush_at": None, "last_error": "", "price_rules": 0, "price_error": "",
                "pending_dir": str(pending.pending_dir()),
                "message": METERING_DISABLED_MESSAGE}
    st = get_writer().status()
    st["message"] = ""
    return st


def health() -> dict:
    """数据库连通性 + 补写目录现状(不抛异常)。"""
    ok, error = db.ping()
    return {"db_ok": ok, "db_error": error, **pending.count(), **status()}
