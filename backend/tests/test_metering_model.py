"""计量模型层:脱敏、事件形态、计价(Decimal / 历史不快照重算)、缓存口径判定。

全部离线,不碰数据库、不碰网络。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.metering import bind_context, build_call, new_id
from app.metering.model import (
    BILLING_BILLABLE, BILLING_UNKNOWN, COST_ESTIMATED, COST_UNKNOWN, LAYER_EMBEDDING,
    NOTE_PRICE_NOT_CONFIGURED, NOTE_PRICE_NOT_LOADED, SERVICE_EMBEDDING, STATUS_FAILURE,
    STATUS_SUCCESS, USAGE_SOURCE_VENDOR, CallEvent, CallItem, CacheEvent,
)
from app.metering.pricing import (
    ESTIMATE_NOTE, MONEY, QUANTITY, PriceBook, PriceRule, amount_of, billing_quantity, resolve,
)
from app.metering.probe import ReadProbe, attempts, classify, prompt_tokens
from app.metering.redact import MAX_ERROR_MESSAGE, safe_endpoint, safe_error, safe_text
from app.metering.writer import ALLOCATION_NOTE, ALLOCATION_NOTE_SINGLE, allocate_items

UTC = timezone.utc
SERVICE_OCR = "ocr"


def _at(days: int = 0) -> datetime:
    return datetime(2026, 3, 1, tzinfo=UTC) + timedelta(days=days)


def _rule(price: str, *, days: int = 0, unit: str = "1k_tokens", target: str = "",
          rid: int = 1, currency: str = "CNY") -> PriceRule:
    return PriceRule(id=rid, service=SERVICE_EMBEDDING, provider="dashscope", target=target,
                     unit=unit, currency=currency, unit_price=Decimal(price),
                     effective_from=_at(days), source="官方价格页 2026-10-03")


# ---- 脱敏 ----

def test_safe_text_masks_credentials_before_truncating():
    msg = ("请求失败 signature=abc123DEFghi&AccessKeyId=LTAI5tSecret Signing "
           "Bearer sk-abcdef123456 password=hunter2 api_key: k-9")
    out = safe_text(msg)
    for leaked in ("abc123DEFghi", "LTAI5tSecret", "sk-abcdef123456", "hunter2", "k-9"):
        assert leaked not in out
    assert "***" in out


def test_safe_text_drops_url_query_string():
    """签名 / 令牌常在查询串里:整段查询串一律抹掉。"""
    out = safe_text("POST https://ocr.cn-shanghai.aliyuncs.com/?Signature=zzz&x=1 failed")
    assert "Signature=zzz" not in out and "?***" in out


def test_safe_text_truncates_and_flattens():
    out = safe_text("行一\n\t行二   " + "长" * 400)
    assert "\n" not in out and "\t" not in out
    assert len(out) <= MAX_ERROR_MESSAGE and out.endswith("…")


def test_safe_endpoint_keeps_host_only():
    assert safe_endpoint("https://dashscope.aliyuncs.com/compatible-mode/v1") == \
        "dashscope.aliyuncs.com"
    assert safe_endpoint("https://ocr.example.com:8443/?Signature=x") == "ocr.example.com:8443"
    assert safe_endpoint("dashscope.aliyuncs.com") == "dashscope.aliyuncs.com"
    assert safe_endpoint("") == ""


def test_safe_error_keeps_class_name_but_masks_message():
    class VendorTimeout(TimeoutError):
        pass

    cls, msg = safe_error(VendorTimeout("read timeout token=abcdef"))
    assert cls == "VendorTimeout"
    assert "abcdef" not in msg


# ---- 事件形态 ----

def test_new_id_is_unique_hex():
    ids = {new_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 32 and int(i, 16) >= 0 for i in ids)


def test_call_event_payload_round_trip_keeps_decimals_and_nulls():
    event = CallEvent(
        event_id="a" * 32, service=SERVICE_EMBEDDING, purpose="document_index",
        provider="dashscope", target="text-embedding-v4", duration_ms=812,
        status=STATUS_SUCCESS, usage_quantity=Decimal("1234.5"), usage_unit="token",
        usage_source=USAGE_SOURCE_VENDOR, billing_status=BILLING_BILLABLE,
        cost_amount=Decimal("0.00063453"), currency="CNY", cost_status=COST_ESTIMATED,
        http_attempts=3, call_group="b" * 32, attempt_no=2, retry_of="c" * 32,
        price_snapshot={"unit_price": "0.000514", "unit": "1k_tokens"},
        items=(CallItem(document_id="d" * 32, oss_key="knowledge/x.pdf", text_count=10),),
    )
    payload = event.as_payload()
    assert payload["http_attempts"] == 3
    assert payload["usage_quantity"] == "1234.5"          # 字符串,绝不经过 float
    assert not any(isinstance(v, float) for v in payload.values() if v is not None)

    back = CallEvent.from_payload(payload)
    assert back == event
    assert back.usage_quantity == Decimal("1234.5")
    assert back.items[0].text_count == 10
    assert back.items[0].item_key.endswith("knowledge/x.pdf")


def test_call_event_keeps_unknown_usage_and_cost_as_null():
    event = CallEvent(event_id="e" * 32, service=SERVICE_OCR, purpose="document_index",
                      provider="aliyun", status=STATUS_FAILURE)
    payload = event.as_payload()
    assert payload["usage_quantity"] is None and payload["cost_amount"] is None
    assert payload["usage_source"] == "unknown"
    back = CallEvent.from_payload(payload)
    assert back.usage_quantity is None and back.cost_amount is None


def test_cache_event_round_trip():
    event = CacheEvent(event_id="f" * 32, layer=LAYER_EMBEDDING, purpose="document_index",
                       unit="text", hit_count=7, miss_count=3, shared_count=1, skipped_count=2,
                       job_id="job-1", note="第 3 层")
    assert CacheEvent.from_payload(event.as_payload()) == event


def test_item_key_is_null_safe():
    """MySQL 唯一索引不挡 NULL:归属字段全空时给占位符,同一事件也只保留一行。"""
    assert CallItem().item_key == "-"
    assert CallItem(document_id="d" * 32).item_key == "d" * 32
    assert CallItem(oss_key="k.pdf").item_key == "k.pdf"
    assert CallItem(document_id="d" * 32, oss_key="k.pdf").item_key == f"{'d' * 32}:k.pdf"


# ---- 计价 ----

def test_billing_quantity_and_amount_are_decimal_exact():
    """3210 token × 0.000514 元/千 token = 0.00164994 元:Decimal 下分毫不差。"""
    qty = billing_quantity(Decimal(3210), "1k_tokens")
    assert qty == Decimal("3.210000") and isinstance(qty, Decimal)
    assert amount_of(qty, Decimal("0.000514")) == Decimal("0.00164994")
    assert billing_quantity(Decimal(1), "request") == Decimal("1.000000")   # 按次不做除法
    assert billing_quantity(None, "1k_tokens") is None                      # 拿不到不猜


def test_money_and_quantity_quantization_bounds():
    """金额 8 位小数、用量 6 位小数:超出部分四舍五入,不会撑破列宽。"""
    assert MONEY == Decimal("0.00000001") and QUANTITY == Decimal("0.000001")
    assert billing_quantity(Decimal("0.0000005"), "1k_tokens") == Decimal("0.000000")


def test_resolve_prefers_latest_rule_effective_at_that_moment():
    rules = [_rule("0.000514", days=0, rid=1), _rule("0.000700", days=10, rid=2)]
    assert resolve(rules, service=SERVICE_EMBEDDING, provider="dashscope", target="",
                   unit="1k_tokens", at=_at(5)).unit_price == Decimal("0.000514")
    assert resolve(rules, service=SERVICE_EMBEDDING, provider="dashscope", target="",
                   unit="1k_tokens", at=_at(11)).unit_price == Decimal("0.000700")
    assert resolve([_rule("9", days=30)], service=SERVICE_EMBEDDING, provider="dashscope",
                   target="", unit="1k_tokens", at=_at(0)) is None    # 未来才生效 → 不参与


def test_resolve_prefers_exact_target_over_generic():
    rules = [_rule("0.000514", target="", rid=1), _rule("0.001", target="text-embedding-v4", rid=2)]
    exact = resolve(rules, service=SERVICE_EMBEDDING, provider="dashscope",
                    target="text-embedding-v4", unit="1k_tokens", at=_at(1))
    assert exact is not None and exact.unit_price == Decimal("0.001") and exact.id == 2
    generic = resolve(rules, service=SERVICE_EMBEDDING, provider="dashscope",
                      target="other-model", unit="1k_tokens", at=_at(1))
    assert generic is not None and generic.id == 1


def test_quote_missing_price_is_unknown_not_zero():
    book = PriceBook(lambda: [], ttl=60)
    q = book.quote(service=SERVICE_EMBEDDING, provider="dashscope", target="m",
                   unit="1k_tokens", at=_at(), usage=Decimal(100))
    assert q.cost is None and q.status == COST_UNKNOWN
    assert q.note == NOTE_PRICE_NOT_CONFIGURED and q.rule is None


def test_quote_without_loaded_prices_says_not_loaded():
    """业务线程不等数据库:价格表没载入时如实记「未载入」,由写库前补价。"""
    book = PriceBook(lambda: [], ttl=60)
    q = book.quote(service=SERVICE_EMBEDDING, provider="dashscope", target="m",
                   unit="1k_tokens", at=_at(), usage=Decimal(100), allow_load=False)
    assert q.note == NOTE_PRICE_NOT_LOADED and q.cost is None


def test_quote_missing_usage_keeps_rule_but_no_amount():
    book = PriceBook(lambda: [_rule("0.000514").snapshot()], ttl=60)
    q = book.quote(service=SERVICE_EMBEDDING, provider="dashscope", target="",
                   unit="1k_tokens", at=_at(1), usage=None)
    assert q.cost is None and q.status == COST_UNKNOWN
    assert q.rule is not None and q.currency == "CNY"      # 规则命中,但没用量 => 不估算
    assert "用量" in q.note


def test_quote_is_marked_estimated_with_source_note():
    book = PriceBook(lambda: [_rule("0.000514").snapshot()], ttl=60)
    q = book.quote(service=SERVICE_EMBEDDING, provider="dashscope", target="",
                   unit="1k_tokens", at=_at(1), usage=Decimal(2000))
    assert q.status == COST_ESTIMATED and q.note == ESTIMATE_NOTE
    assert q.cost == Decimal("0.00102800") and q.currency == "CNY"
    assert q.price_version and q.snapshot is not None


def test_price_version_changes_with_price_but_not_with_id():
    assert _rule("0.000514", rid=1).version != _rule("0.000600", rid=1).version
    assert _rule("0.000514", rid=2).version == _rule("0.000514", rid=1).version


def test_history_is_not_recomputed_after_price_change():
    """改价不静默重算历史:老事件按当时生效的价估算,新事件才用新价。"""
    rows: list[dict] = [_rule("0.000514", days=0, rid=1).snapshot()]
    book = PriceBook(lambda: list(rows), ttl=60)

    event_time = _at(1)
    before = book.quote(service=SERVICE_EMBEDDING, provider="dashscope", target="",
                        unit="1k_tokens", at=event_time, usage=Decimal(1000))
    assert before.cost == Decimal("0.00051400")

    rows.append(_rule("0.001200", days=2, rid=2).snapshot())   # 之后涨价
    book.invalidate()

    after = book.quote(service=SERVICE_EMBEDDING, provider="dashscope", target="",
                       unit="1k_tokens", at=event_time, usage=Decimal(1000))
    assert after.cost == before.cost                       # 同一时刻:金额不变
    assert after.price_version == before.price_version     # 快照仍是老规则
    assert after.rule is not None and after.rule.id == 1

    later = book.quote(service=SERVICE_EMBEDDING, provider="dashscope", target="",
                       unit="1k_tokens", at=_at(3), usage=Decimal(1000))
    assert later.cost == Decimal("0.00120000")             # 新时刻才吃新价


def test_price_book_falls_back_to_last_good_snapshot_on_db_failure():
    """数据库抖动时沿用上次价格表:不因为读不到价就把金额清空。"""
    state = {"fail": False}

    def loader():
        if state["fail"]:
            raise RuntimeError("MySQL 挂了")
        return [_rule("0.000514").snapshot()]

    book = PriceBook(loader, ttl=60)
    assert len(book.rules()) == 1
    state["fail"] = True
    assert len(book.rules(refresh=True)) == 1     # 刷新失败:沿用上次成功的表
    assert "MySQL 挂了" in book.error


def test_price_book_caches_and_only_reloads_after_ttl():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return []

    book = PriceBook(loader, ttl=3600)
    book.rules()
    book.rules()
    assert calls["n"] == 1
    book.rules(refresh=True)
    assert calls["n"] == 2


# ---- 批量分摊 ----

def test_allocate_items_splits_cost_without_double_counting():
    event = CallEvent(event_id="a" * 32, service=SERVICE_EMBEDDING, purpose="document_index",
                      provider="dashscope", cost_amount=Decimal("0.003"),
                      items=(CallItem(oss_key="a.pdf", text_count=3),
                             CallItem(oss_key="b.pdf", text_count=1)))
    out = allocate_items(event)
    shares = [i.allocated_cost for i in out.items]
    assert shares == [Decimal("0.00225000"), Decimal("0.00075000")]
    assert sum(shares) == event.cost_amount                 # 分摊之和 = 事件金额,不翻倍
    assert out.cost_amount == event.cost_amount             # 事件本身金额不变
    assert all(i.allocation_note == ALLOCATION_NOTE for i in out.items)


def test_allocate_items_single_file_is_marked_estimated():
    event = CallEvent(event_id="a" * 32, service=SERVICE_OCR, purpose="document_index",
                      provider="aliyun", cost_amount=Decimal("0.05"),
                      items=(CallItem(oss_key="a.pdf", page_no=2, text_count=1),))
    out = allocate_items(event)
    assert out.items[0].allocated_cost == Decimal("0.05000000")
    assert out.items[0].allocation_note == ALLOCATION_NOTE_SINGLE


def test_allocate_items_without_cost_keeps_ownership_only():
    event = CallEvent(event_id="a" * 32, service=SERVICE_OCR, purpose="document_index",
                      provider="aliyun", cost_amount=None,
                      items=(CallItem(oss_key="a.pdf", text_count=1),))
    out = allocate_items(event)
    assert out.items[0].allocated_cost is None
    assert "无金额" in out.items[0].allocation_note


# ---- 缓存口径 ----

def test_classify_distinguishes_hit_shared_and_miss():
    def probe(calls: int, hits: int) -> ReadProbe:
        p = ReadProbe(lambda: None)
        p.calls, p.hits = calls, hits
        return p

    assert classify(probe(1, 1), True) == {"hit": 1, "miss": 0, "shared": 0}    # 直接读到
    assert classify(probe(2, 1), True) == {"hit": 0, "miss": 0, "shared": 1}    # 等锁后复用
    assert classify(probe(1, 0), False) == {"hit": 0, "miss": 1, "shared": 0}   # 真未命中
    assert classify(probe(0, 0), None) == {"hit": 0, "miss": 0, "shared": 0}    # 没算(抢锁失败)
    assert classify(probe(1, 0), None, attempted=True) == {"hit": 0, "miss": 1, "shared": 0}


def test_read_probe_counts_calls_and_hits():
    values = iter([None, {"v": 1}])
    p = ReadProbe(lambda: next(values))
    assert p() is None and not p.read_hit
    assert p() == {"v": 1} and not p.read_hit      # 第二次才命中 = 复用他人结果
    assert p.calls == 2 and p.hits == 1


def test_probe_helpers_never_fabricate_usage():
    assert attempts([{"status": 200}, {"status": 500}]) == 2
    assert prompt_tokens([{"status": 200}]) is None                    # 没报用量 → None
    assert prompt_tokens([{"usage": {"prompt_tokens": 12}},
                          {"usage": {"input_tokens": 8}}]) == 20


# ---- 构造入口 ----

def test_build_call_uses_context_and_price_book(metering_env):
    metering_env.store.add_price({
        "service": SERVICE_EMBEDDING, "provider": "dashscope", "target": "text-embedding-v4",
        "unit": "1k_tokens", "currency": "CNY", "unit_price": Decimal("0.000514"),
        "effective_from": _at(0).isoformat(), "source": "官方", "note": "",
    })
    metering_env.writer.price_book.rules(refresh=True)

    with bind_context(job_id="job-1", document_id="d" * 32, oss_key="k.pdf", page_no=3):
        event = build_call(service=SERVICE_EMBEDDING, provider="dashscope",
                           target="text-embedding-v4", endpoint="https://x.example.com/v1",
                           duration_ms=120, status=STATUS_SUCCESS,
                           usage_quantity=Decimal(1000), usage_unit="token",
                           usage_source=USAGE_SOURCE_VENDOR, price_unit="1k_tokens")

    assert event.job_id == "job-1" and event.document_id == "d" * 32 and event.page_no == 3
    assert event.status == STATUS_SUCCESS and event.usage_source == USAGE_SOURCE_VENDOR
    assert event.cost_amount == Decimal("0.00051400") and event.currency == "CNY"
    assert event.price_snapshot is not None and event.cost_status == COST_ESTIMATED


def test_build_call_marks_billing_unknown_on_failure(metering_env):
    event = build_call(service=SERVICE_OCR, provider="aliyun", target="Invoice",
                       endpoint="https://ocr.example.com", duration_ms=30, status=STATUS_FAILURE,
                       billing_status=BILLING_UNKNOWN, error_class="OCRError",
                       error_message="Throttling", price_unit="request")
    assert event.usage_quantity is None and event.cost_amount is None
    assert event.billing_status == BILLING_UNKNOWN
    assert event.error_message == "Throttling"
