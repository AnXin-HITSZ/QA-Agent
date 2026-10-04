"""调用链路的计量:embedding 批量 / 部分命中 / 去重 / 查询,OCR 命中 / 强制 / 失败 / 重试。

所有供应商调用都是假的(不触网):embedding 用假内层客户端 + 真探针,
OCR 打桩 ocr._post_once(网络边界)。缓存层用内存缓存,数据库用内存仓库。
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.metering import bind_context, record_call
from app.metering.context import PURPOSE_INDEX
from app.metering.model import (
    BILLING_BILLABLE, BILLING_UNKNOWN, COST_ESTIMATED, COST_UNKNOWN, LAYER_EMBEDDING,
    LAYER_OCR_RAW, LAYER_OCR_TEXT, NOTE_PRICE_NOT_CONFIGURED, SERVICE_EMBEDDING, SERVICE_OCR,
    STATUS_FAILURE, STATUS_SUCCESS, USAGE_SOURCE_LOCAL_COUNT, USAGE_SOURCE_UNKNOWN,
    USAGE_SOURCE_VENDOR, CallItem,
)
from app.metering.probe import UsageProbe
from app.rag import embedding_cache, ingest, ocr, ocr_cache, retrieve
from app.rag.ingest import Options, Scope
from tests import meterkit as mk
from tests.pdfkit import make_pdf


def _flush(env) -> None:
    env.writer.flush()


def _fake(env, probe, **over):
    """假内层客户端 + 计量包装。维度必须对齐配置,否则向量写不进缓存(会被判非法)。"""
    inner = mk.FakeInnerEmbeddings(probe, dim=env.settings.embeddings_dim, **over)
    return inner, mk.metered(inner, probe)


# ---- embedding:按真实请求逐条记账 ----


def test_embedding_splits_batches_into_one_event_per_request(metering_env):
    probe = UsageProbe()
    inner, client = _fake(metering_env, probe, tokens=1000)

    with bind_context(purpose=PURPOSE_INDEX, job_id="job-1", document_id="d" * 32,
                      oss_key="knowledge/a.pdf"):
        vectors = client.embed_documents([f"文本 {i}" for i in range(25)])

    assert len(vectors) == 25
    assert [len(c) for c in inner.calls] == [10, 10, 5]        # 一批 = 一次请求

    _flush(metering_env)
    rows = mk.calls(metering_env.store)
    assert len(rows) == 3
    assert {r["service"] for r in rows} == {SERVICE_EMBEDDING}
    assert {r["purpose"] for r in rows} == {"document_index"}
    assert {r["job_id"] for r in rows} == {"job-1"}
    assert all(r["usage_quantity"] == "1000" for r in rows)     # 每次请求各自的用量
    assert all(r["usage_source"] == USAGE_SOURCE_VENDOR for r in rows)
    assert all(r["http_attempts"] == 1 for r in rows)
    assert sum(int(r["http_attempts"]) for r in rows) == 3      # 实际外部调用 = 3 次
    assert mk.sum_field(rows, "usage_quantity") == Decimal(3000)

    texts = [item["text_count"] for r in rows for item in
             metering_env.store.get_call(r["event_id"])["items"]]
    assert sorted(texts) == [5, 10, 10]                         # 归属到文件,条数准确


def test_embedding_partial_cache_hit_only_submits_misses(metering_env, cache_env):
    """已缓存的文本不再提交:只有未命中的才产生外部调用与费用。"""
    probe = UsageProbe()
    inner, client = _fake(metering_env, probe, tokens=500)

    texts = ["甲", "乙", "丙", "丁"]
    embedding_cache.embed_documents(client, texts)
    assert inner.texts == texts                                 # 首轮:全部未命中
    _flush(metering_env)                                        # 事件还在队列里,先落库再取基线
    first_round = len(mk.calls(metering_env.store))

    inner.calls.clear()
    result = embedding_cache.embed_documents(client, ["甲", "乙", "戊"])   # 2 命中 + 1 新

    assert len(result) == 3
    assert inner.texts == ["戊"]                                # 只提交未命中的那一条
    _flush(metering_env)
    rows = mk.calls(metering_env.store)
    assert len(rows) - first_round == 1
    assert rows[0]["usage_quantity"] == "500"

    stats = mk.cached(metering_env.store, LAYER_EMBEDDING)
    assert sum(s["hit_count"] for s in stats) == 2              # 命中不计费
    assert sum(s["miss_count"] for s in stats) == 4 + 1         # 两次调用各自未命中数


def test_embedding_dedupe_skips_repeats_without_paying_twice(metering_env, cache_env):
    probe = UsageProbe()
    inner, client = _fake(metering_env, probe, tokens=100)

    embedding_cache.embed_documents(client, ["同一条", "同一条", "同一条", "另一条"])
    assert inner.texts == ["同一条", "另一条"]                   # 重复文本只提交一次

    _flush(metering_env)
    stats = mk.cached(metering_env.store, LAYER_EMBEDDING)
    assert sum(s["skipped_count"] for s in stats) == 2          # 去重跳过:不算命中也不算调用
    assert len(mk.calls(metering_env.store)) == 1


def test_all_cached_round_makes_no_call_event(metering_env, cache_env):
    probe = UsageProbe()
    inner, client = _fake(metering_env, probe)

    embedding_cache.embed_documents(client, ["甲", "乙"])
    _flush(metering_env)                                         # 先落库,免得基线漏算队列
    before = len(mk.calls(metering_env.store))
    inner.calls.clear()

    embedding_cache.embed_documents(client, ["甲", "乙"])         # 全命中
    _flush(metering_env)
    assert inner.calls == []                                     # 零外部调用
    assert len(mk.calls(metering_env.store)) == before           # 零新增调用记录
    stats = mk.cached(metering_env.store, LAYER_EMBEDDING)
    assert sum(s["hit_count"] for s in stats) == 2               # 第二轮 2 条全命中
    assert sum(s["miss_count"] for s in stats) == 2              # 只有第一轮的 2 条未命中


def test_search_knowledge_records_query_purpose_without_file_info(metering_env, kb_env,
                                                                 monkeypatch):
    """真实检索入口:向量化记 purpose=query,且不带上一批文件的残留归属信息。"""
    probe = UsageProbe()
    inner, client = _fake(kb_env, probe, tokens=7)
    monkeypatch.setattr(retrieve, "get_embeddings", lambda: client)   # 检索取客户端的地方

    with bind_context(job_id="job-old", document_id="d" * 32, oss_key="knowledge/a.pdf",
                      page_no=3):                                 # 索引线程遗留的上下文
        hits = retrieve.search_knowledge("发票抬头写错了怎么办")

    assert inner.texts == ["发票抬头写错了怎么办"]                 # 空集合:照常调用并记账
    _flush(metering_env)
    row = mk.calls(metering_env.store)[0]
    assert row["purpose"] == "query"                              # 归属:检索发起
    assert (row["job_id"], row["document_id"], row["oss_key"], row["page_no"]) == (None,) * 4
    assert mk.cached(metering_env.store, LAYER_EMBEDDING) == []    # 查询向量不进缓存统计
    assert hits == []                                             # 空集合:无命中


def test_index_job_attributes_calls_to_document_index(metering_env, kb_env, monkeypatch):
    """真跑一次索引任务:归属在**任务入口**绑定,不靠调用方记得先 bind。"""
    probe = UsageProbe()
    inner, client = _fake(kb_env, probe, tokens=300)
    monkeypatch.setattr(ingest, "get_embeddings", lambda: client)   # 索引链路取客户端的地方
    kb_env.kb.files["票据说明.pdf"] = make_pdf(pages=[{"text": "报销标准 见附件"}])

    summary = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options(),
                                   job_id="job-42")

    assert summary["published"] is True
    _flush(metering_env)
    rows = mk.calls(metering_env.store)
    assert rows                                                   # 至少一次向量化调用
    assert {r["purpose"] for r in rows} == {PURPOSE_INDEX}        # 不是默认的 query
    assert {r["job_id"] for r in rows} == {"job-42"}
    assert {r["oss_key"] for r in rows} == {"票据说明.pdf"}
    assert all(r["document_id"] for r in rows)                    # 文件身份一并带上
    assert mk.cached(metering_env.store, LAYER_EMBEDDING)         # 缓存统计也记在这条链路上


def test_sdk_internal_retry_is_reported_as_extra_attempt(metering_env):
    """供应商 SDK 内部重试:一个事件里如实记 2 次请求,不假装只有一次。"""
    probe = UsageProbe()
    inner, client = _fake(metering_env, probe, tokens=400, responses=2)

    client.embed_documents(["a"])
    _flush(metering_env)
    row = mk.calls(metering_env.store)[0]
    assert row["http_attempts"] == 2
    assert "重试" in row["usage_note"]


def test_embedding_failure_keeps_vendor_usage_but_billing_unknown(metering_env):
    """失败但响应里报了用量:用量是事实,照记;是否计费仍记 unknown(不当作免费)。"""
    probe = UsageProbe()
    boom = RuntimeError("upstream 500; token=sk-should-not-leak")
    inner, client = _fake(metering_env, probe, responses=1, status=500, error=boom)
    mk.install_prices(metering_env, mk.price_row(SERVICE_EMBEDDING, "0.001"))

    with pytest.raises(RuntimeError):
        client.embed_documents(["a"])

    _flush(metering_env)
    row = mk.calls(metering_env.store)[0]
    assert row["status"] == STATUS_FAILURE
    assert row["usage_quantity"] == "1000" and row["usage_source"] == USAGE_SOURCE_VENDOR
    assert row["billing_status"] == BILLING_UNKNOWN               # 供应商是否计费未知
    assert row["cost_status"] == COST_ESTIMATED                   # 按报出的用量估算,非账单
    assert row["http_status"] == 500
    assert "sk-should-not-leak" not in row["error_message"]       # 兜底脱敏


def test_embedding_timeout_without_response_is_not_free(metering_env):
    probe = UsageProbe()
    inner, client = _fake(metering_env, probe, responses=0,
                          error=TimeoutError("read timeout"))

    with pytest.raises(TimeoutError):
        client.embed_documents(["a"])

    _flush(metering_env)
    row = mk.calls(metering_env.store)[0]
    assert row["status"] == STATUS_FAILURE
    assert row["http_status"] is None                             # 一个响应都没收到
    assert row["usage_quantity"] is None and row["cost_amount"] is None
    assert row["usage_source"] == USAGE_SOURCE_UNKNOWN
    assert row["billing_status"] == BILLING_UNKNOWN               # 超时 ≠ 免费


def test_embedding_without_vendor_usage_is_unknown_not_guessed(metering_env):
    probe = UsageProbe()
    inner, client = _fake(metering_env, probe, tokens=None)        # 响应里没有 usage

    client.embed_documents(["甲" * 50])
    _flush(metering_env)
    row = mk.calls(metering_env.store)[0]
    assert row["status"] == STATUS_SUCCESS
    assert row["usage_quantity"] is None                          # 不按字数折算
    assert row["usage_source"] == USAGE_SOURCE_UNKNOWN
    assert row["cost_amount"] is None and row["cost_status"] == COST_UNKNOWN
    assert "usage" in row["usage_note"]


@pytest.mark.parametrize("eager", [True, False], ids=["content-ready", "stream-unread"])
def test_usage_probe_reads_real_httpx_responses(eager):
    """探针与真实 httpx 响应对象兼容(生产链路正是这么装的)。

    eager=True 是构造时就带 content 的响应;真 transport 不这样 —— body 留在流里
    (stream=ResponseStream),而 httpx 在钩子**之后**才 read,所以钩子里 response.json()
    会抛 ResponseNotRead,用量只能丢(2026-10-04 线上 embedding 调用全是这个形状)。
    两种构造方式都要覆盖:只测前者的话这个 bug 测不出来。
    """
    from app.metering.probe import prompt_tokens

    probe = UsageProbe()
    payload = {"usage": {"prompt_tokens": 21, "total_tokens": 21}, "data": [{"embedding": [0.1]}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if eager:
            return httpx.Response(200, json=payload)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(payload).encode()))

    client = httpx.Client(transport=httpx.MockTransport(handler),
                          event_hooks={"response": [probe.hook]})
    with probe.collecting() as seen:
        client.post("https://example.com/v1/embeddings", json={"input": ["a"]})

    assert len(seen) == 1 and seen[0]["status"] == 200
    assert prompt_tokens(seen) == 21


def test_probe_leaves_streaming_bodies_to_the_caller():
    """非 JSON(SSE 之类)响应的 body 归调用方:探针只记状态码,不替它把流提前读了。"""
    probe = UsageProbe()
    body = b'data: {"usage": {"prompt_tokens": 1}}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=httpx.ByteStream(body))

    client = httpx.Client(transport=httpx.MockTransport(handler),
                          event_hooks={"response": [probe.hook]})
    with probe.collecting() as seen:
        response = client.send(client.build_request("POST", "https://example.com/v1/chat"),
                               stream=True)

    assert not response.is_stream_consumed          # 流还在,没被探针提前吞掉
    assert b"".join(response.iter_bytes()) == body
    assert seen == [{"status": 200}]


def test_logging_failure_does_not_retry_the_paid_call(metering_env, monkeypatch):
    """计量写不进去时业务照常成功,且**不会**因此重发付费请求。"""
    probe = UsageProbe()
    inner, client = _fake(metering_env, probe, tokens=10)

    def boom(_event):
        raise RuntimeError("写入器炸了")

    monkeypatch.setattr(metering_env.writer, "call", boom)
    vectors = client.embed_documents(["a", "b"])

    assert len(vectors) == 2
    assert len(inner.calls) == 1                                  # 只发了一次


def test_embedding_event_cost_uses_price_book(metering_env):
    probe = UsageProbe()
    inner, client = _fake(metering_env, probe, tokens=2000)
    mk.install_prices(metering_env, mk.price_row(SERVICE_EMBEDDING, "0.000514"))

    client.embed_documents(["a"])
    _flush(metering_env)
    row = mk.calls(metering_env.store)[0]
    assert row["cost_amount"] == "0.00102800"                     # 2000 token × 单价/千
    assert row["cost_status"] == COST_ESTIMATED and row["currency"] == "CNY"
    assert row["price_snapshot"]["unit_price"] == "0.000514"


def test_embedding_without_price_stays_unknown(metering_env):
    """没配价格:如实记「未配置价格」,金额为空,绝不写 0(0 会被读成免费)。"""
    probe = UsageProbe()
    inner, client = _fake(metering_env, probe, tokens=2000)

    client.embed_documents(["a"])
    _flush(metering_env)
    row = mk.calls(metering_env.store)[0]
    assert row["cost_amount"] is None and row["cost_status"] == COST_UNKNOWN
    assert row["cost_note"] == NOTE_PRICE_NOT_CONFIGURED


# ---- OCR:命中不调用、强制重识别、失败与重试 ----


@pytest.fixture
def ocr_env(monkeypatch):
    """OCR 打桩环境:只替换网络边界 _post_once 与退避睡眠。"""
    monkeypatch.setattr(ocr, "_sleep", lambda _s: None)
    return ocr


def _raw_response(request_id: str = "req-1", text: str = "发票 金额 100") -> dict:
    return {
        "RequestId": request_id,
        "Data": {"SubImages": [{"Type": "Invoice", "KvInfo": {"Data": {}},
                                "BlockInfo": {"BlockDetails": [{"BlockContent": text}]}}]},
    }


def test_ocr_miss_records_one_request_with_page_and_file(monkeypatch, metering_env, kb_env,
                                                         ocr_env):
    calls = []
    monkeypatch.setattr(ocr, "_post_once", lambda image, t, timeout: calls.append(t) or
                        _raw_response())

    with bind_context(job_id="job-9", document_id="d" * 32, oss_key="knowledge/a.pdf", page_no=2):
        page = ocr_cache.recognize_cached(b"\x89PNG-p1", "invoice")

    assert page.ocr_hit is False and calls == ["Invoice"]
    _flush(metering_env)
    rows = mk.calls(metering_env.store)
    assert len(rows) == 1
    row = rows[0]
    assert row["service"] == SERVICE_OCR and row["status"] == STATUS_SUCCESS
    assert row["usage_quantity"] == "1" and row["usage_unit"] == "request"
    assert row["usage_source"] == USAGE_SOURCE_LOCAL_COUNT        # 按次:本端计数
    assert row["billing_status"] == BILLING_BILLABLE
    assert row["page_no"] == 2 and row["oss_key"] == "knowledge/a.pdf"
    assert row["job_id"] == "job-9"

    raw_stats = mk.cached(metering_env.store, LAYER_OCR_RAW)
    assert sum(s["miss_count"] for s in raw_stats) == 1
    text_stats = mk.cached(metering_env.store, LAYER_OCR_TEXT)
    assert sum(s["miss_count"] for s in text_stats) == 1          # 本地转换:不算外部调用


def test_ocr_raw_cache_hit_makes_no_external_call(monkeypatch, metering_env, kb_env, ocr_env):
    """第 1 层命中:不调供应商,也就没有调用记录与费用。"""
    calls = []
    monkeypatch.setattr(ocr, "_post_once", lambda image, t, timeout: calls.append(t) or
                        _raw_response())

    ocr_cache.recognize_cached(b"same-image", "invoice")
    _flush(metering_env)
    assert len(mk.calls(metering_env.store)) == 1                 # 首次:一次真实调用

    page = ocr_cache.recognize_cached(b"same-image", "invoice")   # 同图再识别
    _flush(metering_env)

    assert page.ocr_hit is True and len(calls) == 1               # 没有第二次外部调用
    assert len(mk.calls(metering_env.store)) == 1                 # 调用记录没有增加
    raw_stats = mk.cached(metering_env.store, LAYER_OCR_RAW)
    assert sum(s["hit_count"] for s in raw_stats) == 1            # 只记缓存命中
    text_stats = mk.cached(metering_env.store, LAYER_OCR_TEXT)
    assert sum(s["hit_count"] for s in text_stats) == 1           # 转换结果也命中


def test_ocr_refresh_forces_new_request_and_new_call_event(monkeypatch, metering_env, kb_env,
                                                           ocr_env):
    calls = []
    monkeypatch.setattr(ocr, "_post_once", lambda image, t, timeout: calls.append(t) or
                        _raw_response())

    ocr_cache.recognize_cached(b"img", "invoice")
    _flush(metering_env)
    ocr_cache.recognize_cached(b"img", "invoice", refresh=True)    # 强制重新识别
    _flush(metering_env)

    assert len(calls) == 2                                         # 又调了一次
    assert len(mk.calls(metering_env.store)) == 2                  # 也就多记一条
    raw_stats = mk.cached(metering_env.store, LAYER_OCR_RAW)
    assert sum(s["hit_count"] for s in raw_stats) == 0             # 强制刷新不算命中


def test_ocr_retryable_failure_records_each_attempt_and_links_them(monkeypatch, metering_env,
                                                                  kb_env, ocr_env):
    """超时重试:每次真实请求各记一条,共享 call_group,retry_of 串成链路。"""
    attempts = {"n": 0}

    def flaky(image, aliyun_type, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ocr.OCRError("NetworkTimeout", "请求超时(5s)", retryable=True, status=None)
        return _raw_response("req-2")

    monkeypatch.setattr(ocr, "_post_once", flaky)
    raw = ocr.recognize_raw(b"img", "invoice", retries=1)

    assert raw["RequestId"] == "req-2" and attempts["n"] == 2
    _flush(metering_env)
    rows = sorted(mk.calls(metering_env.store), key=lambda r: r["attempt_no"])
    assert [r["status"] for r in rows] == [STATUS_FAILURE, STATUS_SUCCESS]
    assert [r["attempt_no"] for r in rows] == [1, 2]
    assert rows[0]["call_group"] and rows[0]["call_group"] == rows[1]["call_group"]
    assert rows[1]["retry_of"] == rows[0]["event_id"]              # 链路可查
    assert rows[0]["usage_quantity"] is None                       # 失败:用量未知
    assert rows[0]["billing_status"] == BILLING_UNKNOWN            # 是否计费未知,不记 0
    assert rows[0]["cost_amount"] is None
    assert rows[1]["usage_quantity"] == "1" and rows[1]["retry_of"] is not None


def test_ocr_non_retryable_failure_records_once_and_stops(monkeypatch, metering_env, kb_env,
                                                          ocr_env):
    attempts = {"n": 0}

    def denied(image, aliyun_type, timeout):
        attempts["n"] += 1
        raise ocr.OCRError("InvalidAccessKeyId", "签名不对", status=400, retryable=False)

    monkeypatch.setattr(ocr, "_post_once", denied)
    with pytest.raises(ocr.OCRError):
        ocr.recognize_raw(b"img", "invoice", retries=3)

    assert attempts["n"] == 1                                      # 不可重试:不重复计费
    _flush(metering_env)
    rows = mk.calls(metering_env.store)
    assert len(rows) == 1 and rows[0]["status"] == STATUS_FAILURE
    assert rows[0]["error_class"] == "OCRError"
    assert "InvalidAccessKeyId" in rows[0]["error_message"]
    assert rows[0]["http_status"] == 400


def test_ocr_failure_is_not_cached_so_next_call_retries(monkeypatch, metering_env, kb_env,
                                                       ocr_env):
    """失败不写成功缓存:下一次仍然真的去调供应商(而不是命中失败结果)。"""
    state = {"fail": True}

    def flaky(image, aliyun_type, timeout):
        if state["fail"]:
            raise ocr.OCRError("Throttling", "限流", retryable=False, status=429)
        return _raw_response()

    monkeypatch.setattr(ocr, "_post_once", flaky)
    with pytest.raises(ocr.OCRError):
        ocr_cache.recognize_cached(b"img", "invoice")

    state["fail"] = False
    page = ocr_cache.recognize_cached(b"img", "invoice")           # 不会被失败结果挡住
    assert page.ocr_hit is False                                   # 依然是一次真实调用

    _flush(metering_env)
    assert len(mk.calls(metering_env.store)) == 2                  # 两次调用都记了


def test_ocr_timeout_exhausting_retries_marks_billing_unknown(monkeypatch, metering_env, kb_env,
                                                             ocr_env):
    def always_timeout(image, aliyun_type, timeout):
        raise ocr.OCRError("NetworkTimeout", "请求超时(5s)", retryable=True)

    monkeypatch.setattr(ocr, "_post_once", always_timeout)
    with pytest.raises(ocr.OCRError):
        ocr.recognize_raw(b"img", "invoice", retries=1)

    _flush(metering_env)
    rows = mk.calls(metering_env.store)
    assert len(rows) == 2                                          # 两次尝试各一条
    assert all(r["billing_status"] == BILLING_UNKNOWN for r in rows)
    assert all(r["cost_amount"] is None for r in rows)             # 超时不等于免费


def test_ocr_price_estimates_per_request(metering_env, kb_env, ocr_env, monkeypatch):
    monkeypatch.setattr(ocr, "_post_once", lambda image, t, timeout: _raw_response())
    mk.install_prices(metering_env, mk.price_row(SERVICE_OCR, "0.05", unit="request",
                                                 provider="aliyun", days_ago=2))

    ocr_cache.recognize_cached(b"img", "invoice")
    _flush(metering_env)
    row = mk.calls(metering_env.store)[0]
    assert row["cost_amount"] == "0.05000000"                      # 1 次请求 × 单价
    assert row["billing_unit"] == "request" and row["currency"] == "CNY"


def test_multi_file_batch_is_counted_once(metering_env):
    """一次请求覆盖多个文件:金额按文本数分摊,事件只有一条(不重复计数)。"""
    mk.install_prices(metering_env, mk.price_row(SERVICE_EMBEDDING, "0.001"))

    record_call(mk.embedding_call(
        usage_quantity=Decimal(2000), http_attempts=1,
        items=(CallItem(oss_key="a.pdf", text_count=6), CallItem(oss_key="b.pdf", text_count=2)),
    ))
    _flush(metering_env)
    rows = mk.calls(metering_env.store)
    assert len(rows) == 1                                          # 一条事件,不是两条
    detail = metering_env.store.get_call(rows[0]["event_id"])
    assert detail["cost_amount"] == "0.00200000"
    shares = [Decimal(i["allocated_cost"]) for i in detail["items"]]
    assert shares == [Decimal("0.00150000"), Decimal("0.00050000")]
    assert sum(shares) == Decimal("0.00200000")                    # 分摊之和 = 总额
    assert all("估算" in i["allocation_note"] for i in detail["items"])
