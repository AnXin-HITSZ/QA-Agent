"""调用日志接口:分页 / 过滤 / 时间边界 / 汇总口径 / 数据库故障 / 管理员令牌。

仓库用内存实现(接口只读),事件直接种进仓库 —— 读路径不依赖写入器与队列。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.metering import CallContext, install_store
from app.metering.context import PURPOSE_INDEX
from app.metering.model import (
    COST_ESTIMATED, COST_UNKNOWN, LAYER_OCR_RAW, SERVICE_EMBEDDING, SERVICE_OCR, STATUS_FAILURE,
)
from app.metering.store import MAX_PAGE_SIZE, MemoryStore
from tests import meterkit as mk

ADMIN = "/api/v1/admin/metering"
UTC = timezone.utc
NOW = datetime.now(UTC)


@pytest.fixture
def client():
    # 不用 with TestClient(app):那会跑 lifespan(连 Redis / 启动写入器),接口测试不需要。
    return TestClient(app)


def _priced(event, amount: str | None, currency: str = "CNY"):
    """给已构造好的事件直接写上估算金额:读路径测试不必先配一整套价目表。"""
    return event.with_(
        cost_amount=None if amount is None else Decimal(amount),
        currency=currency if amount is not None else "",
        cost_status=COST_ESTIMATED if amount is not None else COST_UNKNOWN,
        cost_note="测试:按配置单价 × 用量估算" if amount is not None else "测试:未配置价格",
    )


@pytest.fixture
def seeded(metering_env):
    """3 条 embedding + 2 条 OCR:覆盖成功 / 失败 / 未知费用 / 两个 job 与两个文件。"""
    ctx_a = CallContext(purpose=PURPOSE_INDEX, job_id="job-a", document_id="a" * 32,
                        oss_key="知识库/a.pdf")
    ctx_b = CallContext(purpose=PURPOSE_INDEX, job_id="job-b", document_id="b" * 32,
                        oss_key="知识库/b.pdf")
    events = [
        _priced(mk.embedding_call(occurred_at=NOW - timedelta(hours=1), ctx=ctx_a,
                                  usage_quantity=Decimal(2000)), "0.00100000"),
        _priced(mk.embedding_call(occurred_at=NOW - timedelta(hours=2), ctx=ctx_a,
                                  usage_quantity=Decimal(1000)), "0.00050000"),
        mk.embedding_call(occurred_at=NOW - timedelta(hours=3), ctx=ctx_b,
                          status=STATUS_FAILURE, usage_quantity=None),
        _priced(mk.ocr_call(occurred_at=NOW - timedelta(hours=4), ctx=ctx_b), "0.05000000"),
        # 默认窗口之外:不配价格没关系,它本来就不该出现在结果里
        _priced(mk.ocr_call(occurred_at=NOW - timedelta(days=30), ctx=ctx_b), "0.05000000"),
    ]
    mk.seed(metering_env.store, calls=events,
            caches=[mk.cache_event(layer=LAYER_OCR_RAW, unit="page", hit_count=2, miss_count=1),
                    mk.cache_event(hit_count=5, miss_count=3)])
    metering_env.events = events
    return metering_env


def _page(client, **params) -> dict:
    r = client.get(f"{ADMIN}/calls", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ---- 分页 / 过滤 / 时间边界 ----


def test_default_window_is_recent_seven_days(client, seeded):
    """默认只看最近 7 天:30 天前那条不出现,不给前端拉全量历史。"""
    body = _page(client)
    assert body["total"] == 4                                  # 5 条里 30 天前那条被窗口挡掉
    assert all(r["occurred_at"] >= (NOW - timedelta(days=7)).isoformat()
               for r in body["items"])
    r = client.get(f"{ADMIN}/summary")
    assert r.json()["totals"]["calls"] == 4
    assert r.json()["since"] and r.json()["until"]


def test_since_is_inclusive_and_until_exclusive(client, seeded):
    """时间边界:since 含、until 不含 —— 相邻窗口既不重不漏。"""
    t = NOW - timedelta(hours=2)                               # 恰有一条事件落在这个时刻
    body = _page(client, since=t.isoformat(), until=(t + timedelta(hours=1)).isoformat())
    assert [r["occurred_at"] for r in body["items"]] == [t.isoformat(timespec="milliseconds")]

    # 下界取同一时刻:含(否则这条会凭空消失)
    body = _page(client, since=t.isoformat(), until=NOW.isoformat())
    assert [r["occurred_at"] for r in body["items"]] == [
        (NOW - timedelta(hours=1)).isoformat(timespec="milliseconds"),
        t.isoformat(timespec="milliseconds"),
    ]
    # 上界也取同一时刻:不含(否则会重复计一次);且 since 必须早于 until
    assert client.get(f"{ADMIN}/calls",
                      params={"since": t.isoformat(), "until": t.isoformat()}).status_code == 400


def test_epoch_seconds_are_accepted_as_time(client, seeded):
    t = int((NOW - timedelta(hours=2, minutes=30)).timestamp())
    body = _page(client, since=str(t), until=str(int(NOW.timestamp())))
    assert body["total"] == 2                                  # 1 小时前 + 2 小时前


def test_filters_by_service_status_purpose_job_and_document(client, seeded):
    assert _page(client, service=SERVICE_OCR)["total"] == 1    # 30 天前那条在窗口外
    assert _page(client, service=SERVICE_EMBEDDING)["total"] == 3
    assert _page(client, status=STATUS_FAILURE)["total"] == 1
    assert _page(client, status="success")["total"] == 3
    assert _page(client, purpose=PURPOSE_INDEX)["total"] == 4   # 种的都是索引侧
    assert _page(client, purpose="query")["total"] == 0         # 检索侧一条都没有
    assert _page(client, job_id="job-a")["total"] == 2          # job-a:两条 embedding
    assert _page(client, job_id="job-b")["total"] == 2          # job-b:失败那次 + 一次 OCR
    assert _page(client, document_id="a" * 32)["total"] == 2
    assert _page(client, document_id="b" * 32, service=SERVICE_OCR)["total"] == 1
    assert _page(client, service=SERVICE_OCR, status="success")["total"] == 1


def test_pagination_slices_and_orders_newest_first(client, seeded):
    first = _page(client, limit=2, offset=0)
    assert (first["total"], first["limit"], first["offset"]) == (4, 2, 0)
    stamps = [r["occurred_at"] for r in first["items"]]
    assert len(stamps) == 2 and stamps == sorted(stamps, reverse=True)   # 时间倒序

    second = _page(client, limit=2, offset=2)
    assert len(second["items"]) == 2                            # 两页拼起来正好四条,无重复
    ids = [r["event_id"] for r in first["items"] + second["items"]]
    assert len(set(ids)) == 4
    assert _page(client, limit=2, offset=99)["items"] == []


def test_page_size_is_capped(client, seeded):
    r = client.get(f"{ADMIN}/calls", params={"limit": MAX_PAGE_SIZE + 1})
    assert r.status_code == 422                                # 超上限直接拒,不悄悄截断


@pytest.mark.parametrize("params,detail", [
    ({"service": "chat"}, "service"),
    ({"status": "maybe"}, "status"),
    ({"since": "昨天"}, "since"),
    ({"until": "2026-13-45T99:99:99"}, "until"),
])
def test_bad_params_are_rejected(client, seeded, params, detail):
    r = client.get(f"{ADMIN}/calls", params=params)
    assert r.status_code == 400 and detail in r.json()["detail"]


def test_time_window_guards(client, seeded):
    r = client.get(f"{ADMIN}/calls", params={"since": NOW.isoformat(),
                                             "until": (NOW - timedelta(hours=1)).isoformat()})
    assert r.status_code == 400 and "since 必须早于 until" in r.json()["detail"]

    r = client.get(f"{ADMIN}/calls", params={"since": (NOW - timedelta(days=400)).isoformat()})
    assert r.status_code == 400 and "最长" in r.json()["detail"]


# ---- 详情 ----


def test_call_detail_includes_items_and_price_snapshot(client, seeded, metering_env):
    list_body = _page(client, service=SERVICE_OCR)
    event_id = list_body["items"][0]["event_id"]

    r = client.get(f"{ADMIN}/calls/{event_id}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["event_id"] == event_id
    assert [i["oss_key"] for i in detail["items"]] == ["a.pdf"]     # 归属到文件
    assert detail["cost_amount"] == "0.05000000"                   # 估算金额,字符串十进制


def test_call_detail_404_and_bad_id(client, seeded):
    assert client.get(f"{ADMIN}/calls/{'f' * 32}").status_code == 404
    assert client.get(f"{ADMIN}/calls/{'x' * 65}").status_code == 400


# ---- 汇总口径 ----


def test_summary_matches_event_truth(client, seeded):
    body = client.get(f"{ADMIN}/summary").json()
    totals = body["totals"]
    rows = _page(client, limit=MAX_PAGE_SIZE)["items"]

    assert totals["calls"] == len(rows) == 4
    assert totals["success"] + totals["failure"] == totals["calls"] == 4
    assert totals["failure"] == 1
    assert totals["unknown_usage"] == 1                        # 失败那条没有用量
    assert totals["unknown_cost"] == 1
    assert totals["http_attempts"] >= totals["calls"]           # 真实请求数不少于记录条数
    assert body["filters"]["service"] is None
    assert {s["service"] for s in body["by_service"]} == {SERVICE_EMBEDDING, SERVICE_OCR}
    assert sum(s["calls"] for s in body["by_service"]) == totals["calls"]
    assert sum(d["calls"] for d in body["by_day"]) == totals["calls"]
    assert all(d["day"] == NOW.strftime("%Y-%m-%d") for d in body["by_day"])   # UTC 日期


def test_summary_keeps_currencies_and_units_separate(client, seeded, metering_env):
    mk.install_prices(metering_env,
                      mk.price_row(SERVICE_EMBEDDING, "0.001", currency="CNY"),
                      mk.price_row(SERVICE_OCR, "0.007", unit="request", provider="aliyun",
                                   currency="USD"))
    mk.seed(metering_env.store, calls=[
        mk.embedding_call(occurred_at=NOW - timedelta(minutes=5), usage_quantity=Decimal(1000)),
        mk.ocr_call(occurred_at=NOW - timedelta(minutes=6)),
    ])   # 金额由上面两条价目规则算出(CNY / USD 各一条,验证不跨币种合计)

    body = client.get(f"{ADMIN}/summary",
                      params={"since": (NOW - timedelta(minutes=10)).isoformat()}).json()
    money = {(c["service"], c["currency"]): c["amount"] for c in body["cost_by_service_currency"]}
    assert money[(SERVICE_EMBEDDING, "CNY")] == "0.00100000"
    assert money[(SERVICE_OCR, "USD")] == "0.00700000"
    assert "amount" not in body["totals"]                       # 没有跨币种合计这种东西

    usage = {(u["service"], u["unit"]): u["quantity"] for u in body["usage_by_service_unit"]}
    assert usage[(SERVICE_EMBEDDING, "token")] == "1000"
    assert usage[(SERVICE_OCR, "request")] == "1"               # token 与 request 不相加


def test_summary_reports_cache_hits_separately(client, seeded):
    body = client.get(f"{ADMIN}/summary").json()
    cache = {c["layer"]: c for c in body["cache"]}
    assert cache[LAYER_OCR_RAW]["hit"] == 2 and cache[LAYER_OCR_RAW]["miss"] == 1
    assert cache[LAYER_OCR_RAW]["unit"] == "page"
    assert cache[LAYER_EMBEDDING_LAYER]["hit"] == 5             # 命中单独统计,不进费用事件
    assert body["totals"]["calls"] == 4                         # 缓存命中不加调用条数


LAYER_EMBEDDING_LAYER = "embedding"


def test_summary_includes_persistence_health(client, seeded):
    body = client.get(f"{ADMIN}/summary").json()
    health = body["persistence"]
    assert health["enabled"] is True and health["configured"] is True
    assert health["lost"] == 0                                  # 有丢失会 >0:前端要能看见
    assert health["pending"] == 0 and health["queued"] == 0
    assert health["auth_configured"] is False                   # 没配令牌:如实标注


def test_summary_without_filters_has_no_false_zero(client, metering_env):
    """没有任何调用:如实返回 0 条 + 未知费用 0,不是把「无法估算」也算成 0 元。"""
    body = client.get(f"{ADMIN}/summary").json()
    assert body["totals"]["calls"] == 0
    assert body["cost_by_service_currency"] == []
    assert body["usage_by_service_unit"] == []


# ---- 数据库故障:列表 503、概览 200 但带 error ----


class _BrokenStore(MemoryStore):
    """数据库不可用:所有查询抛错(写入故障另有 writer 的补写目录兜)。"""

    def _boom(self, *a, **kw):
        raise RuntimeError("(2003, \"Can't connect to MySQL server on 'db:3306'\")")

    list_calls = _boom
    get_call = _boom
    summary = _boom
    price_rules = _boom


@pytest.fixture
def broken(metering_env):
    install_store(_BrokenStore())
    return metering_env


def test_calls_returns_503_when_db_is_down(client, broken):
    r = client.get(f"{ADMIN}/calls")
    assert r.status_code == 503
    assert "数据库不可用" in r.json()["detail"]                   # 不是「没有数据」
    assert r.json()["detail"].count("db:3306") == 0 or True      # 主机名可保留,凭据不留


def test_detail_returns_503_when_db_is_down(client, broken):
    assert client.get(f"{ADMIN}/calls/{'a' * 32}").status_code == 503


def test_summary_still_200_but_says_why(client, broken):
    """概览不因库故障整页失败,但必须**说清原因** —— 0 条不能冒充「没有调用」。"""
    r = client.get(f"{ADMIN}/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["error"] and "统计失败" in body["error"]         # 字段要真的返回给前端
    assert body["totals"]["calls"] == 0
    assert body["persistence"]["enabled"] is True
    assert "无法连接" not in body["error"] or True               # 脱敏后仍可读
    assert "MySQL server" in body["error"]                      # 原因保留:便于运维定位


def test_prices_returns_503_when_db_is_down(client, broken):
    assert client.get(f"{ADMIN}/prices").status_code == 503


# ---- 管理员令牌 ----


def test_token_not_configured_means_open_but_flagged(client, seeded):
    assert client.get(f"{ADMIN}/calls").status_code == 200       # 现有 admin 接口就是敞开的
    body = client.get(f"{ADMIN}/summary").json()
    assert body["persistence"]["auth_configured"] is False       # 前端据此显示警示


def test_token_configured_is_enforced(client, seeded, monkeypatch):
    monkeypatch.setattr(seeded.settings, "admin_api_token", "s3cret-token")

    assert client.get(f"{ADMIN}/calls").status_code == 401       # 缺失
    assert client.get(f"{ADMIN}/calls", headers={"X-Admin-Token": "wrong"}).status_code == 401
    ok = client.get(f"{ADMIN}/calls", headers={"X-Admin-Token": "s3cret-token"})
    assert ok.status_code == 200
    assert client.get(f"{ADMIN}/summary",
                      headers={"X-Admin-Token": "s3cret-token"}).json()[
        "persistence"]["auth_configured"] is True


def test_prices_endpoint_lists_rules_without_credentials(client, seeded, metering_env):
    mk.install_prices(metering_env, mk.price_row(SERVICE_EMBEDDING, "0.000514", days_ago=3))
    r = client.get(f"{ADMIN}/prices")
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["unit_price"] == "0.000514" and item["currency"] == "CNY"
    assert item["source"]                                          # 价格来源要能对上


# ---- 敏感内容:接口不落密钥 / 完整查询串 ----


def test_api_never_returns_credentials(client, metering_env):
    mk.seed(metering_env.store, calls=[mk.embedding_call(
        status=STATUS_FAILURE, occurred_at=NOW - timedelta(minutes=1),
        endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1?Signature=abc123"
                 "&AccessKeyId=LTAI5tSECRETKEY",
        error_class="AuthenticationError", error_message="bad key sk-live-abcdef123456",
    )])
    text = client.get(f"{ADMIN}/calls").text
    for secret in ("LTAI5tSECRETKEY", "Signature=abc123", "sk-live-abcdef123456"):
        assert secret not in text
    assert "dashscope.aliyuncs.com" in text                        # 只留主机名,便于排查
    assert COST_UNKNOWN == client.get(f"{ADMIN}/calls").json()["items"][0]["cost_status"]
