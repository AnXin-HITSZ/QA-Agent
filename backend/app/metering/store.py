"""调用日志仓库:写入(幂等)+ 查询 + 汇总,全部走参数化 SQL / ORM(技术方案 §6 §8)。

写入幂等:call_events 主键 = event_id、cache_events.event_id 唯一、items 唯一
(event_id,item_key)。MySQL 用 INSERT ... ON DUPLICATE KEY UPDATE 一次批量去重;
其它方言(SQLite 测试)退回「整批失败→逐行 SAVEPOINT」,语义相同。

汇总一律在 SQL 里做 group by(不在 Python 里全表拉取);不同币种 / 不同单位分开返回,
不给出跨币种合计(方案 §2)。

MemoryStore 是同接口的内存实现,只用于测试与本地演示(绝不当生产存储)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol

from app.metering.db import session_scope
from app.metering.model import CallEvent, CacheEvent
from app.metering.tables import CallEventItemRow, CallEventRow, CacheEventRow, PriceConfigRow

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 200


class PriceExists(Exception):
    """同一 (服务/供应商/目标/单位/币种/生效时间) 的价目已存在:价目只增不改,重复即拒。"""


@dataclass(frozen=True)
class CallFilter:
    """调用列表 / 汇总共用的过滤条件(时间边界为 UTC,由接口层解析)。"""

    since: datetime | None = None
    until: datetime | None = None
    service: str | None = None
    status: str | None = None
    purpose: str | None = None
    job_id: str | None = None
    document_id: str | None = None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _naive(dt: datetime | None) -> datetime | None:
    return None if dt is None else dt.astimezone(timezone.utc).replace(tzinfo=None)


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:f}"


def _price_dict(row: PriceConfigRow) -> dict:
    """PriceConfigRow → 接口返回形状(金额 / 时间出库即字符串,与别处同口径)。"""
    return {"id": row.id, "service": row.service, "provider": row.provider, "target": row.target,
            "unit": row.unit, "currency": row.currency, "unit_price": _money(row.unit_price),
            "effective_from": _iso(row.effective_from), "source": row.source, "note": row.note}


def _price_key(row: dict) -> tuple:
    """唯一键(与 uq_price_config_rule 同口径):内存实现判重用;时间统一成 ISO 再比。"""
    eff = row.get("effective_from")
    if isinstance(eff, datetime):
        eff = _iso(eff)
    key = tuple(str(row.get(k) or "") for k in
                ("service", "provider", "target", "unit", "currency"))
    return (*key, str(eff or ""))


def _price_out(row: dict) -> dict:
    """内存行 → 接口返回形状:单价一律字符串、时间一律 ISO(与 MysqlStore 同形)。"""
    eff = row.get("effective_from")
    if isinstance(eff, datetime):
        eff = _iso(eff)
    price = row.get("unit_price")
    return {**row, "effective_from": eff,
            "unit_price": price if isinstance(price, str) else _money(price)}


def _event_dict(e: CallEvent, items: list[dict] | None = None) -> dict:
    """CallEvent → 接口返回形状(金额 / 用量用字符串,时间用 ISO UTC)。"""
    out = {
        "event_id": e.event_id, "occurred_at": _iso(e.occurred_at), "service": e.service,
        "purpose": e.purpose, "provider": e.provider, "target": e.target, "endpoint": e.endpoint,
        "call_group": e.call_group or None, "attempt_no": e.attempt_no, "retry_of": e.retry_of,
        "http_attempts": e.http_attempts,
        "duration_ms": e.duration_ms, "status": e.status, "error_class": e.error_class,
        "error_message": e.error_message, "http_status": e.http_status,
        "provider_request_id": e.provider_request_id,
        "usage_quantity": _money(e.usage_quantity), "usage_unit": e.usage_unit,
        "usage_source": e.usage_source, "usage_note": e.usage_note,
        "billing_quantity": _money(e.billing_quantity), "billing_unit": e.billing_unit,
        "billing_status": e.billing_status, "billing_note": e.billing_note,
        "cost_amount": _money(e.cost_amount), "currency": e.currency or None,
        "cost_status": e.cost_status, "cost_note": e.cost_note,
        "price_id": e.price_id, "price_version": e.price_version or None,
        "price_snapshot": e.price_snapshot,
        "job_id": e.job_id, "document_id": e.document_id, "oss_key": e.oss_key, "page_no": e.page_no,
    }
    if items is not None:
        out["items"] = items
    return out


def _row_dict(row: CallEventRow, items: list[dict] | None = None) -> dict:
    return {
        "event_id": row.event_id, "occurred_at": _iso(row.occurred_at), "service": row.service,
        "purpose": row.purpose, "provider": row.provider, "target": row.target,
        "endpoint": row.endpoint, "call_group": row.call_group, "attempt_no": row.attempt_no,
        "retry_of": row.retry_of, "http_attempts": row.http_attempts,
        "duration_ms": row.duration_ms, "status": row.status,
        "error_class": row.error_class, "error_message": row.error_message,
        "http_status": row.http_status, "provider_request_id": row.provider_request_id,
        "usage_quantity": _money(row.usage_quantity), "usage_unit": row.usage_unit,
        "usage_source": row.usage_source, "usage_note": row.usage_note,
        "billing_quantity": _money(row.billing_quantity), "billing_unit": row.billing_unit,
        "billing_status": row.billing_status, "billing_note": row.billing_note,
        "cost_amount": _money(row.cost_amount), "currency": row.currency or None,
        "cost_status": row.cost_status, "cost_note": row.cost_note,
        "price_id": row.price_id, "price_version": row.price_version or None,
        "price_snapshot": row.price_snapshot,
        "job_id": row.job_id, "document_id": row.document_id, "oss_key": row.oss_key,
        "page_no": row.page_no,
        **({"items": items} if items is not None else {}),
    }


def _item_dict(item) -> dict:
    return {
        "document_id": item.document_id, "oss_key": item.oss_key, "page_no": item.page_no,
        "text_count": item.text_count,
        "allocated_cost": _money(item.allocated_cost) if item.allocated_cost is not None
        else item.allocated_cost,
        "allocation_note": item.allocation_note,
    }


class MeteringStore(Protocol):
    """调用日志仓库接口(写入 / 查询 / 汇总 / 价格表)。"""

    def insert(self, events: list[CallEvent], cache_events: list[CacheEvent]) -> int: ...

    def price_rules(self) -> list[dict]: ...

    def insert_price(self, row: dict) -> dict: ...

    def delete_price(self, price_id: int) -> bool: ...

    def health(self) -> dict: ...

    def list_calls(self, flt: CallFilter, *, offset: int, limit: int) -> dict: ...

    def get_call(self, event_id: str) -> dict | None: ...

    def summary(self, flt: CallFilter) -> dict: ...


# ---- MySQL 实现 ----

class MysqlStore:
    """SQLAlchemy 实现:同一份代码可在 MySQL(生产)与 SQLite(测试)上跑。"""

    def insert(self, events: list[CallEvent], cache_events: list[CacheEvent]) -> int:
        """写入(幂等),返回**真正新插入的行数**:重复事件(补写重放)不计入。"""
        n = 0
        with session_scope() as session:
            if events:
                n += self._insert_ignore_duplicates(
                    session, CallEventRow, [self._event_row(e) for e in events], "event_id")
                items = [self._item_row(e, item) for e in events for item in e.items]
                if items:
                    self._insert_ignore_duplicates(session, CallEventItemRow, items,
                                                   ("event_id", "item_key"))
            if cache_events:
                n += self._insert_ignore_duplicates(
                    session, CacheEventRow, [self._cache_row(c) for c in cache_events], "event_id")
        return n

    @staticmethod
    def _insert_ignore_duplicates(session, table, rows: list[dict], key) -> int:
        """幂等批量写入:重放同一事件不会产生重复行(补写 / 重试安全)。

        返回新插入行数。MySQL 的 ON DUPLICATE KEY UPDATE 在「值没变」时受影响行数为 0,
        而这里冲突分支正是把主键写回它自己,所以受影响行数 = 新插入行数,重复事件不计数。
        """
        if not rows:
            return 0
        if session.get_bind().dialect.name == "mysql":
            from sqlalchemy.dialects.mysql import insert as mysql_insert

            first = key if isinstance(key, str) else key[0]
            stmt = mysql_insert(table).values(rows)
            # 冲突即无操作(把冲突行的主键写回自己),补写重放不会覆盖已有内容
            result = session.execute(stmt.on_duplicate_key_update(**{first: stmt.inserted[first]}))
            return max(0, int(result.rowcount or 0))
        from sqlalchemy import insert
        from sqlalchemy.exc import IntegrityError

        try:
            session.execute(insert(table), rows)
            return len(rows)
        except IntegrityError:
            session.rollback()           # 整批回滚,再逐行重试:只跳过已存在的那几行
        inserted = 0
        for row in rows:
            try:
                with session.begin_nested():
                    session.execute(insert(table), row)
                inserted += 1
            except IntegrityError:
                continue
        return inserted

    @staticmethod
    def _event_row(e: CallEvent) -> dict:
        data = e.as_payload()
        data.pop("items", None)
        data.pop("kind", None)
        data.pop("schema", None)
        data["occurred_at"] = _naive(e.occurred_at)
        for key in ("usage_quantity", "billing_quantity", "cost_amount"):
            val = data.get(key)
            data[key] = None if val is None else Decimal(val)
        return data

    @staticmethod
    def _item_row(e: CallEvent, item) -> dict:
        data = item.as_payload()
        data["event_id"] = e.event_id
        val = data.get("allocated_cost")
        data["allocated_cost"] = None if val is None else Decimal(val)
        return data

    @staticmethod
    def _cache_row(c: CacheEvent) -> dict:
        data = c.as_payload()
        data.pop("kind", None)
        data.pop("schema", None)
        data["occurred_at"] = _naive(c.occurred_at)
        return data

    def price_rules(self) -> list[dict]:
        from sqlalchemy import select

        with session_scope() as session:
            rows = session.execute(select(PriceConfigRow)).scalars().all()
        # 金额与别处同口径:出库即字符串(接口层声明的是字符串,Decimal 直接出去会 500)
        return [_price_dict(r) for r in rows]

    def insert_price(self, row: dict) -> dict:
        """新增一条价目(只增):唯一键冲突 → PriceExists,绝不覆盖既有价格。"""
        from sqlalchemy.exc import IntegrityError

        data = dict(row)
        data["effective_from"] = _naive(data["effective_from"])
        with session_scope() as session:
            obj = PriceConfigRow(**data)
            session.add(obj)
            try:
                session.flush()          # 唯一键冲突在这里暴露;flush 后 id / created_at 已就位
            except IntegrityError as exc:
                raise PriceExists("同一规则(服务/供应商/目标/单位/币种/生效时间)已存在") from exc
            return _price_dict(obj)

    def delete_price(self, price_id: int) -> bool:
        """删除一条价目;返回是否真的删掉了(False = 本来就不存在)。"""
        from sqlalchemy import delete

        with session_scope() as session:
            result = session.execute(delete(PriceConfigRow).where(PriceConfigRow.id == price_id))
            return bool(result.rowcount)

    def health(self) -> dict:
        from sqlalchemy import func, select

        with session_scope() as session:
            calls = session.execute(select(func.count()).select_from(CallEventRow)).scalar() or 0
            cache = session.execute(select(func.count()).select_from(CacheEventRow)).scalar() or 0
            prices = session.execute(select(func.count()).select_from(PriceConfigRow)).scalar() or 0
        return {"table_calls": int(calls), "table_cache_events": int(cache),
                "table_price_rules": int(prices)}

    # ---- 查询 ----

    @staticmethod
    def _where(flt: CallFilter, table=CallEventRow) -> list:
        conds = []
        if flt.since is not None:
            conds.append(table.occurred_at >= _naive(flt.since))
        if flt.until is not None:
            conds.append(table.occurred_at < _naive(flt.until))
        if table is CallEventRow:
            if flt.service:
                conds.append(CallEventRow.service == flt.service)
            if flt.status:
                conds.append(CallEventRow.status == flt.status)
        if flt.purpose:
            conds.append(table.purpose == flt.purpose)
        if flt.job_id:
            conds.append(table.job_id == flt.job_id)
        if table is CallEventRow and flt.document_id:
            conds.append(CallEventRow.document_id == flt.document_id)
        return conds

    def list_calls(self, flt: CallFilter, *, offset: int = 0, limit: int = 50) -> dict:
        from sqlalchemy import func, select

        offset = max(0, int(offset))
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        conds = self._where(flt)
        with session_scope() as session:
            total = session.execute(
                select(func.count()).select_from(CallEventRow).where(*conds)
            ).scalar() or 0
            rows = session.execute(
                select(CallEventRow).where(*conds)
                .order_by(CallEventRow.occurred_at.desc(), CallEventRow.event_id.desc())
                .offset(offset).limit(limit)
            ).scalars().all()
        return {"total": int(total), "offset": offset, "limit": limit,
                "items": [_row_dict(r) for r in rows]}

    def get_call(self, event_id: str) -> dict | None:
        from sqlalchemy import select

        with session_scope() as session:
            row = session.get(CallEventRow, event_id)
            if row is None:
                return None
            items = session.execute(
                select(CallEventItemRow).where(CallEventItemRow.event_id == event_id)
            ).scalars().all()
        return _row_dict(row, [
            {"document_id": i.document_id, "oss_key": i.oss_key, "page_no": i.page_no,
             "text_count": i.text_count, "allocated_cost": _money(i.allocated_cost),
             "allocation_note": i.allocation_note}
            for i in items
        ])

    def summary(self, flt: CallFilter) -> dict:
        """SQL 侧聚合:总量 / 按服务 / 按天 / 按币种 / 缓存命中(不在 Python 里全表拉)。"""
        from sqlalchemy import func, select

        conds = self._where(flt)
        cache_conds = self._where(flt, CacheEventRow)
        out: dict[str, Any] = {}
        with session_scope() as session:
            agg = session.execute(select(
                func.count(CallEventRow.event_id),
                func.sum(_if(CallEventRow.status == "success", 1, 0)),
                func.sum(_if(CallEventRow.usage_quantity.is_(None), 1, 0)),
                func.sum(_if(CallEventRow.cost_amount.is_(None), 1, 0)),
                func.sum(_if(CallEventRow.billing_status == "unknown", 1, 0)),
                func.sum(CallEventRow.http_attempts),
            ).where(*conds)).one()
            calls = int(agg[0] or 0)
            success = int(agg[1] or 0)
            out["totals"] = {
                "calls": calls, "success": success, "failure": calls - success,
                "unknown_usage": int(agg[2] or 0), "unknown_cost": int(agg[3] or 0),
                "billing_unknown": int(agg[4] or 0),
                # 真实发出的 HTTP 请求数:≥ 记录条数(供应商 SDK 内部重试也计入)
                "http_attempts": int(agg[5] or 0),
            }
            by_service = session.execute(select(
                CallEventRow.service, func.count(CallEventRow.event_id),
                func.sum(_if(CallEventRow.status == "success", 1, 0)),
                func.sum(_if(CallEventRow.usage_quantity.is_(None), 1, 0)),
                func.sum(_if(CallEventRow.cost_amount.is_(None), 1, 0)),
                func.sum(CallEventRow.http_attempts),
            ).where(*conds).group_by(CallEventRow.service)).all()
            out["by_service"] = [
                {"service": r[0], "calls": int(r[1] or 0), "success": int(r[2] or 0),
                 "failure": int(r[1] or 0) - int(r[2] or 0),
                 "unknown_usage": int(r[3] or 0), "unknown_cost": int(r[4] or 0),
                 "http_attempts": int(r[5] or 0)}
                for r in by_service
            ]
            by_day = session.execute(select(
                func.date(CallEventRow.occurred_at), CallEventRow.service,
                func.count(CallEventRow.event_id),
                func.sum(_if(CallEventRow.status == "failure", 1, 0)),
            ).where(*conds).group_by(func.date(CallEventRow.occurred_at), CallEventRow.service)
                .order_by(func.date(CallEventRow.occurred_at))).all()
            out["by_day"] = [
                {"day": str(r[0]), "service": r[1], "calls": int(r[2] or 0), "failure": int(r[3] or 0)}
                for r in by_day
            ]
            money = session.execute(select(
                CallEventRow.service, CallEventRow.currency, func.count(CallEventRow.event_id),
                func.sum(CallEventRow.cost_amount),
            ).where(*conds, CallEventRow.cost_amount.is_not(None))
                .group_by(CallEventRow.service, CallEventRow.currency)).all()
            out["cost_by_service_currency"] = [
                {"service": r[0], "currency": r[1], "events": int(r[2] or 0),
                 "amount": _money(Decimal(str(r[3] or 0)))}
                for r in money
            ]
            usage = session.execute(select(
                CallEventRow.service, CallEventRow.usage_unit,
                func.sum(CallEventRow.usage_quantity),
            ).where(*conds, CallEventRow.usage_quantity.is_not(None))
                .group_by(CallEventRow.service, CallEventRow.usage_unit)).all()
            out["usage_by_service_unit"] = [
                {"service": r[0], "unit": r[1], "quantity": _money(r[2])} for r in usage
            ]
            cache_rows = session.execute(select(
                CacheEventRow.layer, CacheEventRow.unit, func.sum(CacheEventRow.hit_count),
                func.sum(CacheEventRow.miss_count), func.sum(CacheEventRow.shared_count),
                func.sum(CacheEventRow.skipped_count),
            ).where(*cache_conds).group_by(CacheEventRow.layer, CacheEventRow.unit)).all()
            out["cache"] = [
                {"layer": r[0], "unit": r[1], "hit": int(r[2] or 0), "miss": int(r[3] or 0),
                 "shared": int(r[4] or 0), "skipped": int(r[5] or 0)}
                for r in cache_rows
            ]
        return out


def _if(cond, yes, no):
    """跨方言条件求和:MySQL / SQLite 都有 iif?统一用 CASE WHEN,两个方言都支持。"""
    from sqlalchemy import case

    return case((cond, yes), else_=no)


# ---- 内存实现(仅测试 / 本地演示) ----

class MemoryStore:
    """进程内实现:接口与 MysqlStore 一致,供测试与无 MySQL 的本地演示。"""

    def __init__(self) -> None:
        self.calls: dict[str, dict] = {}
        self.items: dict[str, list[dict]] = {}
        self.cache_events: dict[str, dict] = {}
        self.prices: list[dict] = []
        self._price_seq = 0           # 自增 id:删掉再增不重号(与 MySQL AUTO_INCREMENT 同语义)
        self.fail_writes = False      # 测试用:模拟数据库不可用

    def _next_price_id(self) -> int:
        self._price_seq += 1
        return self._price_seq

    def insert(self, events: list[CallEvent], cache_events: list[CacheEvent]) -> int:
        if self.fail_writes:
            raise RuntimeError("MemoryStore: 模拟数据库不可用")
        n = 0
        for e in events:
            if e.event_id in self.calls:      # 幂等:重复事件不覆盖
                continue
            self.calls[e.event_id] = _event_dict(e)
            self.items[e.event_id] = [_item_dict(i) for i in e.items]
            n += 1
        for c in cache_events:
            if c.event_id in self.cache_events:
                continue
            self.cache_events[c.event_id] = c.as_payload()
            n += 1
        return n

    def add_price(self, row: dict) -> None:
        """装一条价目(测试用,绕过唯一键检查):字段与 price_config 表的行一致。"""
        self.prices.append({"id": self._next_price_id(), **row})

    def price_rules(self) -> list[dict]:
        # 与 MysqlStore 同形:单价以字符串出库(payload 里存的是 Decimal 也不影响读回)
        return [_price_out(r) for r in self.prices]

    def insert_price(self, row: dict) -> dict:
        if self.fail_writes:
            raise RuntimeError("MemoryStore: 模拟数据库不可用")
        key = _price_key(row)
        if any(_price_key(r) == key for r in self.prices):
            raise PriceExists("同一规则(服务/供应商/目标/单位/币种/生效时间)已存在")
        item = {"id": self._next_price_id(), **row}
        self.prices.append(item)
        return _price_out(item)

    def delete_price(self, price_id: int) -> bool:
        if self.fail_writes:
            raise RuntimeError("MemoryStore: 模拟数据库不可用")
        before = len(self.prices)
        self.prices = [r for r in self.prices if r.get("id") != price_id]
        return len(self.prices) < before

    def health(self) -> dict:
        return {"table_calls": len(self.calls), "table_cache_events": len(self.cache_events),
                "table_price_rules": len(self.prices)}

    def list_calls(self, flt: CallFilter, *, offset: int = 0, limit: int = 50) -> dict:
        rows = [r for r in self.calls.values() if _match(r, flt)]
        rows.sort(key=lambda r: (r["occurred_at"] or "", r["event_id"]), reverse=True)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        return {"total": len(rows), "offset": offset, "limit": limit,
                "items": rows[offset: offset + limit]}

    def get_call(self, event_id: str) -> dict | None:
        row = self.calls.get(event_id)
        if row is None:
            return None
        return {**row, "items": self.items.get(event_id, [])}

    def summary(self, flt: CallFilter) -> dict:
        rows = [r for r in self.calls.values() if _match(r, flt)]
        out = _empty_summary()
        out["totals"]["calls"] = len(rows)
        out["totals"]["success"] = sum(1 for r in rows if r["status"] == "success")
        out["totals"]["failure"] = out["totals"]["calls"] - out["totals"]["success"]
        out["totals"]["unknown_usage"] = sum(1 for r in rows if r["usage_quantity"] is None)
        out["totals"]["unknown_cost"] = sum(1 for r in rows if r["cost_amount"] is None)
        out["totals"]["billing_unknown"] = sum(1 for r in rows if r["billing_status"] == "unknown")
        out["totals"]["http_attempts"] = sum(int(r.get("http_attempts") or 1) for r in rows)

        svc: dict[str, dict] = {}
        for r in rows:
            b = svc.setdefault(r["service"], {"service": r["service"], "calls": 0, "success": 0,
                                              "failure": 0, "unknown_usage": 0, "unknown_cost": 0,
                                              "http_attempts": 0})
            b["calls"] += 1
            b["success" if r["status"] == "success" else "failure"] += 1
            if r["usage_quantity"] is None:
                b["unknown_usage"] += 1
            if r["cost_amount"] is None:
                b["unknown_cost"] += 1
            b["http_attempts"] += int(r.get("http_attempts") or 1)
        out["by_service"] = sorted(svc.values(), key=lambda b: b["service"])

        days: dict[tuple, dict] = {}
        for r in rows:
            day = (r["occurred_at"] or "")[:10]
            b = days.setdefault((day, r["service"]),
                                {"day": day, "service": r["service"], "calls": 0, "failure": 0})
            b["calls"] += 1
            if r["status"] == "failure":
                b["failure"] += 1
        out["by_day"] = sorted(days.values(), key=lambda b: (b["day"], b["service"]))

        money: dict[tuple, dict] = {}
        for r in rows:
            if r["cost_amount"] is None:
                continue
            b = money.setdefault((r["service"], r["currency"]),
                                 {"service": r["service"], "currency": r["currency"],
                                  "events": 0, "amount": Decimal("0")})
            b["events"] += 1
            b["amount"] += Decimal(r["cost_amount"])
        out["cost_by_service_currency"] = [
            {**b, "amount": _money(b["amount"])} for b in money.values()
        ]

        usage: dict[tuple, Decimal] = {}
        for r in rows:
            if r["usage_quantity"] is None:
                continue
            key = (r["service"], r["usage_unit"])
            usage[key] = usage.get(key, Decimal("0")) + Decimal(r["usage_quantity"])
        out["usage_by_service_unit"] = [
            {"service": k[0], "unit": k[1], "quantity": _money(v)} for k, v in usage.items()
        ]

        layers: dict[tuple, dict] = {}
        for c in self.cache_events.values():
            if not _match_cache(c, flt):
                continue
            key = (c["layer"], c.get("unit") or "")
            b = layers.setdefault(key, {"layer": c["layer"], "unit": key[1], "hit": 0, "miss": 0,
                                        "shared": 0, "skipped": 0})
            for k in ("hit", "miss", "shared", "skipped"):
                b[k] += int(c.get(f"{k}_count") or 0)
        out["cache"] = sorted(layers.values(), key=lambda b: (b["layer"], b["unit"]))
        return out


def _match(row: dict, flt: CallFilter) -> bool:
    if flt.since and (row["occurred_at"] or "") < _iso(flt.since):
        return False
    if flt.until and (row["occurred_at"] or "") >= _iso(flt.until):
        return False
    if flt.service and row["service"] != flt.service:
        return False
    if flt.status and row["status"] != flt.status:
        return False
    if flt.purpose and row["purpose"] != flt.purpose:
        return False
    if flt.job_id and row["job_id"] != flt.job_id:
        return False
    if flt.document_id and row["document_id"] != flt.document_id:
        return False
    return True


def _match_cache(row: dict, flt: CallFilter) -> bool:
    if flt.since and (row["occurred_at"] or "") < _iso(flt.since):
        return False
    if flt.until and (row["occurred_at"] or "") >= _iso(flt.until):
        return False
    if flt.job_id and row["job_id"] != flt.job_id:
        return False
    return True


def _empty_summary() -> dict:
    return {"totals": {"calls": 0, "success": 0, "failure": 0, "unknown_usage": 0,
                       "unknown_cost": 0, "billing_unknown": 0, "http_attempts": 0},
            "by_service": [], "by_day": [], "cost_by_service_currency": [],
            "usage_by_service_unit": [], "cache": []}


_store: MeteringStore | None = None


def get_store() -> MeteringStore:
    """当前仓库:默认 MySQL 实现;测试可 install() 换内存实现。"""
    global _store
    if _store is None:
        _store = MysqlStore()
    return _store


def install(store: MeteringStore | None) -> None:
    """显式安装仓库(None = 恢复默认)。仅测试使用。"""
    global _store
    _store = store


def default_since(days: int = 7) -> datetime:
    """默认时间窗:最近 N 天(前端首屏不拉全量历史)。"""
    return datetime.now(timezone.utc) - timedelta(days=days)
