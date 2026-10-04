"""价格配置与估算:配置在库里,快照在事件上,历史不许被改价静默重算(技术方案 §5)。

规则:
- 价格按 服务 / 供应商 / 模型或 OCR Type / 计费单位 / 币种 配置,各自带 effective_from;
- 事件发生时,取「当时生效」的规则,把规则内容快照进事件行 —— 之后改价 / 加价目表,
  历史事件的快照原样保留(预估金额不重算);
- 缺价格就是「无法估算」,不是 0,也不是免费;缺用量同样只报 unknown;
- 全程 Decimal:价格、用量换算、金额都用 Decimal,绝不经过 float;
- 币种 / 单位不混算:每个事件只带一个币种、一个计费单位,汇总按币种分列(方案 §2)。

本模块不做汇率换算、不扣免费额度 / 折扣 —— 那需要官方账单,本期不接入。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable

from app.metering.model import (
    COST_ESTIMATED, COST_UNKNOWN, NOTE_PRICE_NOT_CONFIGURED, NOTE_PRICE_NOT_LOADED,
)

logger = logging.getLogger(__name__)

MONEY = Decimal("0.00000001")     # DECIMAL(18,8)
QUANTITY = Decimal("0.000001")    # DECIMAL(20,6)
ESTIMATE_NOTE = "按配置单价 × 用量估算,非官方账单"

# 计费单位:
#   1k_tokens  按千 token 计价(embedding)
#   request    按成功调用次数计价(OCR)
#   page       按页计价(供应商若按页计费时使用)
UNITS = ("1k_tokens", "request", "page")


@dataclass(frozen=True)
class PriceRule:
    id: int | None
    service: str
    provider: str
    target: str          # 模型名 / OCR Type;空串 = 该服务 / 供应商的通用价
    unit: str
    currency: str
    unit_price: Decimal
    effective_from: datetime
    source: str = ""
    note: str = ""

    @property
    def version(self) -> str:
        """规则内容摘要:改价 / 改币种 / 改生效时间都会变,便于对账时核对。"""
        raw = "|".join([self.service, self.provider, self.target, self.unit, self.currency,
                        str(self.unit_price), self.effective_from.isoformat()])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def snapshot(self) -> dict:
        return {
            "id": self.id, "service": self.service, "provider": self.provider,
            "target": self.target, "unit": self.unit, "currency": self.currency,
            "unit_price": str(self.unit_price),
            "effective_from": self.effective_from.isoformat(),
            "source": self.source, "note": self.note, "version": self.version,
        }

    @classmethod
    def from_row(cls, row: dict) -> "PriceRule":
        eff = row["effective_from"]
        if isinstance(eff, str):
            eff = datetime.fromisoformat(eff)
        if eff.tzinfo is None:
            eff = eff.replace(tzinfo=timezone.utc)
        return cls(
            id=row.get("id"), service=str(row["service"]), provider=str(row["provider"]),
            target=str(row.get("target") or ""), unit=str(row["unit"]),
            currency=str(row["currency"]), unit_price=Decimal(str(row["unit_price"])),
            effective_from=eff, source=str(row.get("source") or ""), note=str(row.get("note") or ""),
        )


@dataclass(frozen=True)
class Quote:
    """一次调用的计价结果(金额可空 = 无法估算,原因写在 note)。"""

    rule: PriceRule | None
    cost: Decimal | None
    currency: str
    billing_quantity: Decimal | None
    billing_unit: str
    status: str            # estimated / unknown
    note: str

    @property
    def price_id(self) -> int | None:
        return self.rule.id if self.rule else None

    @property
    def price_version(self) -> str:
        return self.rule.version if self.rule else ""

    @property
    def snapshot(self) -> dict | None:
        return self.rule.snapshot() if self.rule else None


def billing_quantity(usage: Decimal | None, unit: str) -> Decimal | None:
    """用量 → 计费数量(1k_tokens 要除以 1000);拿不到用量返回 None,绝不猜。"""
    if usage is None:
        return None
    if unit == "1k_tokens":
        return (Decimal(usage) / Decimal(1000)).quantize(QUANTITY, rounding=ROUND_HALF_UP)
    return Decimal(usage).quantize(QUANTITY, rounding=ROUND_HALF_UP)


def amount_of(quantity: Decimal | None, unit_price: Decimal) -> Decimal | None:
    if quantity is None:
        return None
    return (Decimal(quantity) * Decimal(unit_price)).quantize(MONEY, rounding=ROUND_HALF_UP)


def resolve(rules: list[PriceRule], *, service: str, provider: str, target: str, unit: str,
            at: datetime) -> PriceRule | None:
    """取事件发生时刻生效的规则:先精确匹配 target,再退回该服务的通用价(target 为空)。"""
    at = at.astimezone(timezone.utc)
    for wanted in (target, ""):
        best: PriceRule | None = None
        for r in rules:
            if (r.service, r.provider, r.unit, r.target) != (service, provider, unit, wanted):
                continue
            if r.effective_from > at:
                continue
            if best is None or (r.effective_from, r.id or 0) > (best.effective_from, best.id or 0):
                best = r
        if best is not None:
            return best
    return None


class PriceBook:
    """价格表的内存缓存:懒加载 + 定时刷新;数据库故障时退回上一次成功的快照。"""

    def __init__(self, loader: Callable[[], list[dict]], *, ttl: float = 60.0) -> None:
        self._loader = loader
        self._ttl = max(1.0, ttl)
        self._lock = threading.Lock()
        self._rules: list[PriceRule] = []
        self._loaded_at = 0.0
        self._error = ""

    @property
    def error(self) -> str:
        return self._error

    @property
    def loaded(self) -> bool:
        return self._loaded_at > 0

    def rules(self, *, refresh: bool = False) -> list[PriceRule]:
        now = time.monotonic()
        if not refresh and self.loaded and now - self._loaded_at < self._ttl:
            return self._rules
        with self._lock:
            now = time.monotonic()
            if not refresh and self.loaded and now - self._loaded_at < self._ttl:
                return self._rules
            try:
                self._rules = [PriceRule.from_row(r) for r in self._loader()]
                self._loaded_at = now
                self._error = ""
            except Exception as exc:  # noqa: BLE001 —— 库不可用:沿用旧表并记录
                self._error = f"{type(exc).__name__}: {exc}"
                logger.warning("价格表载入失败(沿用上次成功的 %d 条):%s", len(self._rules), exc)
            return self._rules

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    def cached_rules(self) -> list[PriceRule]:
        """只读内存里已有的价格表(业务线程用,不触发数据库读)。"""
        return self._rules if self.loaded else []

    def quote(self, *, service: str, provider: str, target: str, unit: str, at: datetime,
              usage: Decimal | None, allow_load: bool = True) -> Quote:
        rules = self.rules() if allow_load else self.cached_rules()
        rule = resolve(rules, service=service, provider=provider, target=target, unit=unit, at=at)
        if rule is None and not allow_load and not self.loaded:
            return Quote(None, None, "", None, unit, COST_UNKNOWN, NOTE_PRICE_NOT_LOADED)
        if rule is None:
            return Quote(None, None, "", None, unit, COST_UNKNOWN, NOTE_PRICE_NOT_CONFIGURED)
        qty = billing_quantity(usage, rule.unit)
        if qty is None:
            return Quote(rule, None, rule.currency, None, rule.unit, COST_UNKNOWN,
                         "缺供应商报的用量,无法估算")
        return Quote(rule, amount_of(qty, rule.unit_price), rule.currency, qty, rule.unit,
                     COST_ESTIMATED, ESTIMATE_NOTE)
