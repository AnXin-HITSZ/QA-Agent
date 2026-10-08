"""重排序客户端:请求结构、两种响应形态、非法条目、降级与计量口径(§8 / §13)。

HTTP 是被换掉的(httpx.Client 替身),所以这里验证的是**我们这侧的契约**:
发出的 body 长什么样、拿回来的响应怎么映射、失败怎么如实标记。
真实供应商的响应形态不在离线测试里冒充已验证 —— 见技术方案「未验证事项」。
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.memory import rerank
from app.metering import bind_context
from app.metering.context import PURPOSE_MEMORY_SEARCH
from app.metering.metered_llm import NOTE_NO_USAGE
from app.metering.model import (
    BILLING_BILLABLE, BILLING_UNKNOWN, COST_ESTIMATED, NOTE_FAILED_BILLING, SERVICE_RERANK,
    STATUS_FAILURE, STATUS_SUCCESS, USAGE_SOURCE_VENDOR,
)
from tests import meterkit as mk

URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
MODEL = "qwen3.7-text-rerank"
KEY = "sk-test-not-a-real-key"


@pytest.fixture
def rerank_env(monkeypatch):
    """配置齐全的重排序环境:假 HTTP,不触网、不计费。"""
    s = get_settings()
    monkeypatch.setattr(s, "rerank_enabled", True)
    monkeypatch.setattr(s, "rerank_api_url", URL)
    monkeypatch.setattr(s, "rerank_api_key", KEY)
    monkeypatch.setattr(s, "rerank_model", MODEL)
    monkeypatch.setattr(s, "rerank_timeout_seconds", 15.0)
    return s


class _FakeResponse:
    def __init__(self, payload: dict | None, status: int = 200) -> None:
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("响应不是 JSON")
        return self._payload


class _Transport:
    """httpx.Client 的替身:记录每次请求,按脚本依次返回(未一条重复最后一条)。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.init_kwargs: list[dict] = []
        self.responses: list = []

    def script(self, *responses) -> "_Transport":
        self.responses.extend(responses)
        return self

    def install(self, monkeypatch) -> "_Transport":
        def factory(**kwargs):
            self.init_kwargs.append(kwargs)
            return self

        monkeypatch.setattr(httpx, "Client", factory)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "body": json, "headers": headers or {}})
        entry = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(entry, BaseException):
            raise entry
        return _FakeResponse(*entry)


def _ok(*pairs, usage: int | None = 1000) -> tuple[dict, int]:
    """嵌套形态的 200 响应:pairs 是 (index, score),按给的顺序返回。"""
    payload = {"output": {"results": [{"index": i, "relevance_score": s} for i, s in pairs]},
               "request_id": "req-1"}
    if usage is not None:
        payload["usage"] = {"total_tokens": usage}
    return payload, 200


def _docs(n: int) -> list[str]:
    return [f"记忆正文 {i}" for i in range(n)]


# ---- 配置判断 ----


def test_not_configured_says_so_instead_of_pretending(rerank_env, monkeypatch):
    monkeypatch.setattr(rerank_env, "rerank_enabled", False)
    assert not rerank.configured() and "已关闭" in rerank.config_note()

    monkeypatch.setattr(rerank_env, "rerank_enabled", True)
    monkeypatch.setattr(rerank_env, "rerank_api_key", "")
    assert not rerank.configured() and "未配置" in rerank.config_note()


def test_calling_without_configuration_is_a_programming_error(rerank_env, monkeypatch):
    monkeypatch.setattr(rerank_env, "rerank_api_key", "")
    with pytest.raises(RuntimeError, match="未配置"):
        rerank.rerank("查询", _docs(2))


def test_over_the_vendor_document_limit_raises_instead_of_silently_truncating(rerank_env):
    with pytest.raises(ValueError, match="500"):
        rerank.rerank("查询", _docs(rerank.MAX_DOCUMENTS + 1))


def test_empty_documents_make_no_request(rerank_env, monkeypatch):
    transport = _Transport().install(monkeypatch)

    outcome = rerank.rerank("查询", [])

    assert outcome.order == [] and outcome.error == ""
    assert transport.calls == []                    # 没有文档就绝不发请求(不发空请求付费)


# ---- 请求结构 ----


def test_request_body_matches_dashscope_text_rerank_shape(rerank_env, monkeypatch):
    transport = _Transport().script(_ok((1, 0.9), (0, 0.1))).install(monkeypatch)

    rerank.rerank("报销流程", _docs(2), top_n=2)

    sent = transport.calls[0]
    assert sent["url"] == URL
    assert sent["headers"]["Authorization"] == f"Bearer {KEY}"
    assert sent["body"]["model"] == MODEL
    assert sent["body"]["input"] == {"query": "报销流程", "documents": _docs(2)}
    assert sent["body"]["parameters"]["top_n"] == 2
    assert "instruct" not in sent["body"]["parameters"]     # 留空 = 用供应商默认


def test_instruct_is_sent_when_given_and_top_n_is_clamped(rerank_env, monkeypatch):
    transport = _Transport().script(_ok((0, 0.5))).install(monkeypatch)

    rerank.rerank("查询", _docs(3), top_n=99, instruct="retrieve conflicting memories")

    params = transport.calls[0]["body"]["parameters"]
    assert params["top_n"] == 3                              # 不能超过实际文档数
    assert params["instruct"] == "retrieve conflicting memories"


def test_timeout_passed_to_http_client_has_a_floor(rerank_env, monkeypatch):
    transport = _Transport().script(_ok((0, 0.5))).install(monkeypatch)
    monkeypatch.setattr(rerank_env, "rerank_timeout_seconds", 0.05)

    rerank.rerank("查询", _docs(1))

    assert transport.init_kwargs[0]["timeout"] == rerank.HTTP_TIMEOUT_MIN
    assert transport.init_kwargs[0]["follow_redirects"] is False


# ---- 响应解析:两种形态都认,但只信 index ----


def test_parses_nested_output_results_in_model_order(rerank_env, monkeypatch):
    _Transport().script(_ok((2, 0.91), (0, 0.42))).install(monkeypatch)

    outcome = rerank.rerank("查询", _docs(3))

    assert outcome.ok
    assert outcome.order == [2, 0]                   # 保持模型给的顺序
    assert outcome.scores == {2: 0.91, 0: 0.42}
    assert outcome.usage_tokens == 1000 and outcome.request_id == "req-1"


def test_parses_flat_cohere_style_results(rerank_env, monkeypatch):
    """有的网关把 results 放顶层:两处都认,仍然只用 index 映射。"""
    flat = ({"results": [{"index": 1, "relevance_score": 0.7}, {"index": 0, "score": 0.2}]}, 200)
    _Transport().script(flat).install(monkeypatch)

    outcome = rerank.rerank("查询", _docs(2))

    assert outcome.order == [1, 0] and outcome.scores == {1: 0.7, 0: 0.2}


def test_illegal_entries_are_ignored_and_counted(rerank_env, monkeypatch):
    """越界 / 重复 / 缺分数 / 类型不对:宁可少一条,也不猜、不错配到别的记忆上。"""
    payload = ({"output": {"results": [
        {"index": 0, "relevance_score": 0.9},
        {"index": 99, "relevance_score": 0.8},          # 越界
        {"index": 0, "relevance_score": 0.5},           # 重复
        {"index": 1},                                    # 没有分数
        {"index": True, "relevance_score": 0.7},         # bool 不是下标
        {"index": 1, "relevance_score": "0.6"},          # 分数不是数字
        "不是对象",
        {"index": 1, "relevance_score": float("nan")},   # 非有限
    ]}}, 200)
    _Transport().script(payload).install(monkeypatch)

    outcome = rerank.rerank("查询", _docs(3))

    assert outcome.order == [0] and outcome.ignored == 7
    assert outcome.ok


def test_response_without_usable_entries_is_a_failure_not_an_empty_success(rerank_env,
                                                                           monkeypatch):
    _Transport().script(({"output": {"results": [{"index": 42, "relevance_score": 0.5}]}},
                         200)).install(monkeypatch)

    outcome = rerank.rerank("查询", _docs(2))

    assert not outcome.ok and "没有可用的 index" in outcome.error


def test_non_json_body_is_treated_as_a_failure(rerank_env, monkeypatch):
    _Transport().script((None, 200)).install(monkeypatch)

    outcome = rerank.rerank("查询", _docs(2))

    assert not outcome.ok and "没有 results" in outcome.error


# ---- 失败与降级 ----


def test_http_error_keeps_only_the_code_not_the_response_body(rerank_env, monkeypatch,
                                                              metering_env):
    """错误响应体可能回显正文:只能取 code,绝不能写进日志 / 调用记录。"""
    body = {"code": "Throttling.RateQuota", "message": "用户偏好把结果导出为CSV格式SECRET"}
    _Transport().script((body, 429)).install(monkeypatch)

    with bind_context(purpose=PURPOSE_MEMORY_SEARCH):
        outcome = rerank.rerank("查询", _docs(2))

    assert not outcome.ok
    assert "HTTP 429" in outcome.error and "Throttling.RateQuota" in outcome.error
    assert "SECRET" not in outcome.error
    metering_env.writer.flush()
    row = mk.calls(metering_env.store)[0]
    assert "SECRET" not in repr(row)                 # 整行扫一遍,没有正文/响应体
    assert row["status"] == STATUS_FAILURE and row["http_status"] == 429
    assert row["billing_status"] == BILLING_UNKNOWN  # 失败不猜计费
    assert row["usage_quantity"] is None and NOTE_NO_USAGE in row["usage_note"]
    assert NOTE_FAILED_BILLING in row["billing_note"]
    assert "Throttling.RateQuota" in row["usage_note"]


def test_timeout_returns_a_failure_and_records_it(rerank_env, monkeypatch, metering_env):
    _Transport().script(httpx.ConnectTimeout("连接超时")).install(monkeypatch)

    with bind_context(purpose=PURPOSE_MEMORY_SEARCH):
        outcome = rerank.rerank("查询", _docs(2))

    assert not outcome.ok and "ConnectTimeout" in outcome.error
    metering_env.writer.flush()
    row = mk.calls(metering_env.store)[0]
    assert row["status"] == STATUS_FAILURE and row["http_status"] is None
    assert row["error_class"] == "ConnectTimeout" and row["http_attempts"] == 1
    assert row["usage_quantity"] is None and row["billing_status"] == BILLING_UNKNOWN


# ---- 计量:每次真实请求一条事件 ----


def test_records_one_rerank_event_with_vendor_usage(rerank_env, monkeypatch, metering_env):
    _Transport().script(_ok((0, 0.9), usage=1000)).install(monkeypatch)
    mk.install_prices(metering_env, mk.price_row(SERVICE_RERANK, "0.0005", target=MODEL,
                                                 provider="dashscope.aliyuncs.com"))

    with bind_context(purpose=PURPOSE_MEMORY_SEARCH):
        rerank.rerank("查询", _docs(2))

    metering_env.writer.flush()
    rows = mk.calls(metering_env.store)
    assert len(rows) == 1
    row = rows[0]
    assert row["service"] == SERVICE_RERANK and row["purpose"] == "memory_search"
    assert row["provider"] == "dashscope.aliyuncs.com" and row["target"] == MODEL
    assert row["endpoint"] == "dashscope.aliyuncs.com"
    assert row["status"] == STATUS_SUCCESS and row["http_attempts"] == 1  # 不自动重试
    assert row["usage_quantity"] == "1000" and row["usage_source"] == USAGE_SOURCE_VENDOR
    assert row["billing_unit"] == "1k_tokens"
    assert row["cost_amount"] == "0.00050000" and row["cost_status"] == COST_ESTIMATED
    assert row["billing_status"] == BILLING_BILLABLE
    assert row["provider_request_id"] == "req-1"


def test_missing_usage_is_recorded_as_unknown_not_zero(rerank_env, monkeypatch, metering_env):
    _Transport().script(_ok((0, 0.9), usage=None)).install(monkeypatch)

    with bind_context(purpose=PURPOSE_MEMORY_SEARCH):
        outcome = rerank.rerank("查询", _docs(2))

    assert outcome.ok and outcome.usage_tokens is None    # 排序可用,用量未知
    metering_env.writer.flush()
    row = mk.calls(metering_env.store)[0]
    assert row["usage_quantity"] is None                 # 未知就是不写,绝不记 0
    assert NOTE_NO_USAGE in row["usage_note"]
    assert row["cost_amount"] is None


def test_ignored_entries_are_noted_on_the_call_row(rerank_env, monkeypatch, metering_env):
    payload = ({"output": {"results": [{"index": 0, "relevance_score": 0.9},
                                       {"index": 77, "relevance_score": 0.1}]}}, 200)
    _Transport().script(payload).install(monkeypatch)

    with bind_context(purpose=PURPOSE_MEMORY_SEARCH):
        rerank.rerank("查询", _docs(2))

    metering_env.writer.flush()
    assert "忽略 1 条非法排序条目" in mk.calls(metering_env.store)[0]["usage_note"]
