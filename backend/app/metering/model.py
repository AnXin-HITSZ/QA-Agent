"""调用事件 / 缓存事件的数据形态:两层之间的唯一交换格式(入队、落盘、入库都用它)。

设计要点(对应技术方案 §3 §4):
- 一条 call 事件 = **一次真实的外部请求尝试**(HTTP 请求),不是一个业务动作:
  批量向量化内部按 10 条一批拆成多次请求,就记多条事件,费用按请求分别估算;
- 同一逻辑调用的多次尝试(超时重试)共享 call_group,各自有独立 event_id 与
  attempt_no,retry_of 指向上一次尝试 —— 重试链路可查,又不会把两次尝试算成一次;
- http_attempts = 这一行背后的真实 HTTP 请求次数:我们自己的重试循环逐次记行(每行 1),
  供应商 SDK 内部重试无法拆分时,同一行里如实记 N —— 统计「实际外部调用」按它求和;
- 一个请求覆盖多个切块 / 文件时,用 items(关联表)记录归属;每个文件的金额是
  按文本数比例分摊的**估算值**,不算两遍;
- usage / cost 允许为空:拿不到就置空并写明原因,绝不用「0」冒充「不知道」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

SCHEMA_VERSION = 1

# service
SERVICE_EMBEDDING = "embedding"
SERVICE_OCR = "ocr"

# status
STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"

# usage_source:用量从哪里来
USAGE_SOURCE_VENDOR = "vendor_response"   # 供应商响应里明确报的用量(embedding 的 token)
USAGE_SOURCE_LOCAL_COUNT = "local_count"  # 本端计数:供应商不报用量,但次数就是这次请求本身(OCR 按次)
USAGE_SOURCE_UNKNOWN = "unknown"          # 拿不到 —— 置空,不猜

# billing_status:这次调用在供应商侧是否计费
BILLING_BILLABLE = "billable"     # 供应商成功返回,按正常计费
BILLING_UNKNOWN = "unknown"       # 超时 / 无响应 / 错误码:是否计费未知,不记 0

# cost_status
COST_ESTIMATED = "estimated"      # 按单价 × 用量估算(非官方账单)
COST_UNKNOWN = "unknown"          # 缺用量或缺价格,无法估算

# 缓存层
LAYER_OCR_RAW = "ocr_raw"         # 第 1 层:识别结果(命中 = 没调供应商)
LAYER_OCR_TEXT = "ocr_text"       # 第 2 层:文本转换(命中 = 本地转换,本来就不调供应商)
LAYER_EMBEDDING = "embedding"     # 第 3 层:切块向量

# 缓存命中口径的备注(供前端解释,不是错误)
HIT_NOTE_READ = "cache_hit"          # 直接读到缓存:省下一次外部调用
SHARED_NOTE_WAIT = "shared_result"   # 等他方算完复用:同样没有外部调用,但不是「缓存已存在」

# 价格缺失 / 未载入时的原因文案(界面显示「无法估算」的依据)
NOTE_PRICE_NOT_CONFIGURED = "未配置价格"
NOTE_PRICE_NOT_LOADED = "价格表未载入"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    # 微秒精度:队列 / 补写文件 → 数据库是同一份 payload,截到毫秒会让「走补写的记录」
    # 与「正常入库的记录」时间精度不一致(列本身是 DATETIME(6))。
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds") if dt else None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


@dataclass(frozen=True)
class CallItem:
    """一次请求归属到某个文件 / 页面的估算分摊(关联表一行)。"""

    document_id: str | None = None
    oss_key: str | None = None
    page_no: int | None = None
    text_count: int = 0
    allocated_cost: Decimal | None = None
    allocation_note: str = ""

    @property
    def item_key(self) -> str:
        """关联表去重键:同一事件里同一文件只保留一行(NULL 用占位符,MySQL 唯一索引不挡 NULL)。"""
        if self.oss_key:
            return self.oss_key if not self.document_id else f"{self.document_id}:{self.oss_key}"
        return self.document_id or "-"

    def with_(self, **changes) -> "CallItem":
        return replace(self, **changes)

    def as_payload(self) -> dict:
        return {
            "document_id": self.document_id, "oss_key": self.oss_key, "page_no": self.page_no,
            "text_count": int(self.text_count), "item_key": self.item_key,
            "allocated_cost": None if self.allocated_cost is None else str(self.allocated_cost),
            "allocation_note": self.allocation_note,
        }

    @classmethod
    def from_payload(cls, data: dict) -> "CallItem":
        return cls(
            document_id=data.get("document_id") or None,
            oss_key=data.get("oss_key") or None,
            page_no=data.get("page_no"),
            text_count=int(data.get("text_count") or 0),
            allocated_cost=_dec(data.get("allocated_cost")),
            allocation_note=data.get("allocation_note") or "",
        )


@dataclass(frozen=True)
class CallEvent:
    """一次真实外部请求的调用记录。"""

    event_id: str
    service: str
    purpose: str
    provider: str
    target: str = ""                     # embedding 模型名 / OCR 的供应商 Type
    occurred_at: datetime = field(default_factory=utcnow)
    duration_ms: int = 0
    status: str = STATUS_SUCCESS
    error_class: str = ""
    error_message: str = ""
    http_status: int | None = None
    provider_request_id: str | None = None
    endpoint: str = ""
    call_group: str = ""                 # 同一逻辑调用的多次尝试共享
    attempt_no: int = 1
    retry_of: str | None = None
    http_attempts: int = 1               # 这一行对应的真实 HTTP 请求次数(供应商 SDK 内部重试也算)
    usage_quantity: Decimal | None = None
    usage_unit: str = ""
    usage_source: str = USAGE_SOURCE_UNKNOWN
    usage_note: str = ""
    billing_quantity: Decimal | None = None
    billing_unit: str = ""
    billing_status: str = BILLING_UNKNOWN
    billing_note: str = ""
    cost_amount: Decimal | None = None
    currency: str = ""
    cost_status: str = COST_UNKNOWN
    cost_note: str = ""
    price_id: int | None = None
    price_version: str = ""
    price_snapshot: dict | None = None
    job_id: str | None = None
    document_id: str | None = None
    oss_key: str | None = None
    page_no: int | None = None
    items: tuple[CallItem, ...] = ()

    def with_(self, **changes) -> "CallEvent":
        return replace(self, **changes)

    def as_payload(self) -> dict:
        """JSON 安全的完整形态(入队 / 落盘 / 入库共用)。金额用字符串,不用 float。"""
        data = {
            "schema": SCHEMA_VERSION, "kind": "call",
            "event_id": self.event_id, "service": self.service, "purpose": self.purpose,
            "provider": self.provider, "target": self.target,
            "occurred_at": _iso(self.occurred_at), "duration_ms": int(self.duration_ms),
            "status": self.status, "error_class": self.error_class,
            "error_message": self.error_message, "http_status": self.http_status,
            "provider_request_id": self.provider_request_id, "endpoint": self.endpoint,
            "call_group": self.call_group or None, "attempt_no": int(self.attempt_no),
            "retry_of": self.retry_of, "http_attempts": max(1, int(self.http_attempts)),
            "usage_quantity": None if self.usage_quantity is None else str(self.usage_quantity),
            "usage_unit": self.usage_unit, "usage_source": self.usage_source,
            "usage_note": self.usage_note,
            "billing_quantity": None if self.billing_quantity is None else str(self.billing_quantity),
            "billing_unit": self.billing_unit, "billing_status": self.billing_status,
            "billing_note": self.billing_note,
            "cost_amount": None if self.cost_amount is None else str(self.cost_amount),
            "currency": self.currency, "cost_status": self.cost_status, "cost_note": self.cost_note,
            "price_id": self.price_id, "price_version": self.price_version or None,
            "price_snapshot": self.price_snapshot,
            "job_id": self.job_id, "document_id": self.document_id, "oss_key": self.oss_key,
            "page_no": self.page_no,
            "items": [i.as_payload() for i in self.items],
        }
        return data

    @classmethod
    def from_payload(cls, data: dict) -> "CallEvent":
        return cls(
            event_id=data["event_id"], service=data["service"], purpose=data.get("purpose", ""),
            provider=data.get("provider", ""), target=data.get("target", ""),
            occurred_at=_dt(data.get("occurred_at")) or utcnow(),
            duration_ms=int(data.get("duration_ms") or 0),
            status=data.get("status", STATUS_SUCCESS),
            error_class=data.get("error_class") or "", error_message=data.get("error_message") or "",
            http_status=data.get("http_status"),
            provider_request_id=data.get("provider_request_id") or None,
            endpoint=data.get("endpoint") or "",
            call_group=data.get("call_group") or "", attempt_no=int(data.get("attempt_no") or 1),
            retry_of=data.get("retry_of") or None,
            http_attempts=max(1, int(data.get("http_attempts") or 1)),
            usage_quantity=_dec(data.get("usage_quantity")), usage_unit=data.get("usage_unit") or "",
            usage_source=data.get("usage_source") or USAGE_SOURCE_UNKNOWN,
            usage_note=data.get("usage_note") or "",
            billing_quantity=_dec(data.get("billing_quantity")),
            billing_unit=data.get("billing_unit") or "",
            billing_status=data.get("billing_status") or BILLING_UNKNOWN,
            billing_note=data.get("billing_note") or "",
            cost_amount=_dec(data.get("cost_amount")), currency=data.get("currency") or "",
            cost_status=data.get("cost_status") or COST_UNKNOWN, cost_note=data.get("cost_note") or "",
            price_id=data.get("price_id"), price_version=data.get("price_version") or "",
            price_snapshot=data.get("price_snapshot"),
            job_id=data.get("job_id") or None, document_id=data.get("document_id") or None,
            oss_key=data.get("oss_key") or None, page_no=data.get("page_no"),
            items=tuple(CallItem.from_payload(i) for i in (data.get("items") or [])),
        )


@dataclass(frozen=True)
class CacheEvent:
    """一次缓存查表结果的聚合记录(与费用事件分表,不重复计费)。"""

    event_id: str
    layer: str
    purpose: str
    unit: str                       # page / text —— 「命中 N 页 / N 条」的单位
    hit_count: int = 0              # 直接读到缓存:省下一次外部调用(或本地转换)
    miss_count: int = 0             # 未命中,需要计算 / 调用
    shared_count: int = 0           # 未命中但等到了他方的结果(同样没有本次外部调用)
    skipped_count: int = 0          # 去重后不再单独计算的重复项(不算命中,也不算调用)
    occurred_at: datetime = field(default_factory=utcnow)
    job_id: str | None = None
    document_id: str | None = None
    oss_key: str | None = None
    page_no: int | None = None
    note: str = ""

    def as_payload(self) -> dict:
        return {
            "schema": SCHEMA_VERSION, "kind": "cache",
            "event_id": self.event_id, "layer": self.layer, "purpose": self.purpose,
            "unit": self.unit, "hit_count": int(self.hit_count),
            "miss_count": int(self.miss_count), "shared_count": int(self.shared_count),
            "skipped_count": int(self.skipped_count),
            "occurred_at": _iso(self.occurred_at),
            "job_id": self.job_id, "document_id": self.document_id, "oss_key": self.oss_key,
            "page_no": self.page_no, "note": self.note,
        }

    @classmethod
    def from_payload(cls, data: dict) -> "CacheEvent":
        return cls(
            event_id=data["event_id"], layer=data.get("layer", ""), purpose=data.get("purpose", ""),
            unit=data.get("unit", ""), hit_count=int(data.get("hit_count") or 0),
            miss_count=int(data.get("miss_count") or 0),
            shared_count=int(data.get("shared_count") or 0),
            skipped_count=int(data.get("skipped_count") or 0),
            occurred_at=_dt(data.get("occurred_at")) or utcnow(),
            job_id=data.get("job_id") or None, document_id=data.get("document_id") or None,
            oss_key=data.get("oss_key") or None, page_no=data.get("page_no"),
            note=data.get("note") or "",
        )


def payload_kind(data: dict) -> str:
    """补写目录里的记录类型;无法识别返回空串(调用方跳过并告警)。"""
    kind = data.get("kind")
    if kind in ("call", "cache"):
        return str(kind)
    return ""


def dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
