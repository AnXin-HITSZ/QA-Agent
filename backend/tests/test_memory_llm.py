"""记忆用的聊天客户端:计量口径(§13)与工厂接线(§7「不另建未受管理的调用路径」)。

模型是假的(不打探针=收不到响应),计量仓库是内存实现 —— 不触网、不计费。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from app.config import get_settings
from app.metering import bind_context
from app.metering.context import PURPOSE_MEMORY_EXTRACT
from app.metering.metered_llm import NOTE_NO_USAGE, MeteredChat
from app.metering.model import (
    BILLING_BILLABLE, BILLING_UNKNOWN, COST_ESTIMATED, COST_UNKNOWN, NOTE_FAILED_BILLING,
    NOTE_SDK_RETRY, SERVICE_LLM, STATUS_FAILURE, STATUS_SUCCESS, USAGE_SOURCE_UNKNOWN,
    USAGE_SOURCE_VENDOR,
)
from app.metering.probe import UsageProbe
from tests import meterkit as mk


@pytest.fixture
def memory_llm():
    """工厂有 lru_cache:每个用例前后都清一次,免得配置变更串味。"""
    from app.memory import llm as factory

    factory.get_memory_llm.cache_clear()
    yield factory
    factory.get_memory_llm.cache_clear()


def _client(probe: UsageProbe, **over):
    inner = mk.FakeInnerChat(probe, **over)
    return inner, mk.metered_chat(inner, probe)


# ---- 计量:一次 invoke 一条事件 ----


def test_records_one_llm_event_with_vendor_usage(metering_env):
    probe = UsageProbe()
    inner, client = _client(probe, content='{"facts": []}', usage=(120, 30, 150))
    mk.install_prices(metering_env, mk.price_row(SERVICE_LLM, "0.001", target="deepseek-chat",
                                                 provider="api.deepseek.com"))

    with bind_context(purpose=PURPOSE_MEMORY_EXTRACT, job_id="job-1"):
        message = client.invoke([HumanMessage(content="我习惯用中文写实验记录")])

    assert message.content == '{"facts": []}'                      # 业务结果原样透传
    metering_env.writer.flush()
    rows = mk.calls(metering_env.store)
    assert len(rows) == 1                                          # 不重复统计
    row = rows[0]
    assert row["service"] == SERVICE_LLM and row["purpose"] == "memory_extract"
    assert row["provider"] == "api.deepseek.com" and row["target"] == "deepseek-chat"
    assert row["job_id"] == "job-1"
    assert row["status"] == STATUS_SUCCESS and row["http_attempts"] == 1
    assert row["usage_quantity"] == "150" and row["usage_unit"] == "token"
    assert row["usage_source"] == USAGE_SOURCE_VENDOR
    assert row["billing_unit"] == "1k_tokens"                      # 按千 token 计价
    assert row["cost_amount"] == "0.00015000"                      # 150/1000 × 0.001
    assert row["cost_status"] == COST_ESTIMATED
    assert row["billing_status"] == BILLING_BILLABLE
    assert "输入 120" in row["usage_note"] and "输出 30" in row["usage_note"]


def test_usage_falls_back_to_raw_response_when_metadata_missing(metering_env):
    """langchain 没把 usage 归一到 usage_metadata 时,从原始响应兜底 —— 仍然只报供应商的数字。"""
    probe = UsageProbe()
    inner, client = _client(probe, usage=(10, 5, 15), message_usage=False)

    client.invoke([HumanMessage(content="x")])

    metering_env.writer.flush()
    row = mk.calls(metering_env.store)[0]
    assert row["usage_quantity"] == "15" and row["usage_source"] == USAGE_SOURCE_VENDOR
    assert "合计 15" in row["usage_note"]


def test_no_usage_anywhere_is_unknown_not_guessed(metering_env):
    probe = UsageProbe()
    inner, client = _client(probe, usage=None)                     # 响应里根本没有 usage

    client.invoke([HumanMessage(content="x")])

    metering_env.writer.flush()
    row = mk.calls(metering_env.store)[0]
    assert row["status"] == STATUS_SUCCESS
    assert row["usage_quantity"] is None                           # 不按字数折算
    assert row["usage_source"] == USAGE_SOURCE_UNKNOWN
    assert row["cost_amount"] is None and row["cost_status"] == COST_UNKNOWN
    assert NOTE_NO_USAGE in row["usage_note"]


def test_sdk_internal_retry_is_reported(metering_env):
    probe = UsageProbe()
    inner, client = _client(probe, responses=2)

    client.invoke([HumanMessage(content="x")])

    metering_env.writer.flush()
    row = mk.calls(metering_env.store)[0]
    assert row["http_attempts"] == 2                               # 观测到的真实请求次数
    assert NOTE_SDK_RETRY in row["usage_note"]


def test_failure_is_recorded_and_not_free(metering_env):
    """超时:一个响应都没收到 → 用量与费用都写 unknown,绝不记 0。"""
    probe = UsageProbe()
    inner, client = _client(probe, responses=0, error=TimeoutError("read timeout"))

    with pytest.raises(TimeoutError):
        client.invoke([HumanMessage(content="x")])

    metering_env.writer.flush()
    row = mk.calls(metering_env.store)[0]
    assert row["status"] == STATUS_FAILURE
    assert row["http_status"] is None
    assert row["usage_quantity"] is None and row["cost_amount"] is None
    assert row["billing_status"] == BILLING_UNKNOWN
    assert NOTE_FAILED_BILLING in row["billing_note"]
    assert row["error_class"] == "TimeoutError"


def test_failure_with_vendor_usage_records_the_fact_but_unknown_billing(metering_env):
    """失败但响应真报了用量:用量照记(事实),是否计费仍记 unknown。"""
    probe = UsageProbe()
    inner, client = _client(probe, usage=(40, 0, 40), status=500,
                            error=RuntimeError("upstream 500"))

    with pytest.raises(RuntimeError):
        client.invoke([HumanMessage(content="x")])

    metering_env.writer.flush()
    row = mk.calls(metering_env.store)[0]
    assert row["status"] == STATUS_FAILURE and row["http_status"] == 500
    assert row["usage_quantity"] == "40" and row["usage_source"] == USAGE_SOURCE_VENDOR
    assert row["billing_status"] == BILLING_UNKNOWN


def test_record_failure_never_breaks_the_call(metering_env, monkeypatch):
    """记账失败只告警:一次已成功的付费调用绝不因此变成「业务重试」。"""
    from app.metering import metered_llm

    def boom(event):
        raise RuntimeError("写入器不可用")

    monkeypatch.setattr(metered_llm, "record_call", boom)
    probe = UsageProbe()
    inner, client = _client(probe, content="ok")

    message = client.invoke([HumanMessage(content="x")])

    assert message.content == "ok"
    assert len(inner.calls) == 1                                   # 没有第二次调用


def test_passthrough_when_metering_disabled(monkeypatch):
    """关闭计量时逐层透传:不记录、不改行为。"""
    monkeypatch.setattr(get_settings(), "metering_enabled", False)
    probe = UsageProbe()
    inner, client = _client(probe, content="ok")

    assert client.invoke([HumanMessage(content="x")]).content == "ok"
    assert inner.calls                                                 # 业务调用照常发生


# ---- 工厂接线:JSON 模式 / 温度 / 复用现有 LLM_* 配置 ----


def test_factory_uses_llm_config_with_json_mode(memory_llm, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_api_key", "test-key")
    monkeypatch.setattr(s, "memory_llm_json_mode", True)

    client = memory_llm.get_memory_llm()
    assert isinstance(client, MeteredChat)

    # 直接看请求 payload:确认「复用 LLM_* 配置 + 温度 0 + JSON 模式」真的进了请求
    payload = client._inner._get_request_payload([HumanMessage(content="hi")])
    assert payload["model"] == s.llm_model
    assert payload["temperature"] == 0.0                           # 结构化任务:可复现
    assert payload["response_format"] == {"type": "json_object"}


def test_json_mode_can_be_switched_off(memory_llm, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_api_key", "test-key")
    monkeypatch.setattr(s, "memory_llm_json_mode", False)

    client = memory_llm.get_memory_llm()
    payload = client._inner._get_request_payload([HumanMessage(content="hi")])

    assert "response_format" not in payload                        # 端点不支持时不带


def test_factory_without_key_raises(memory_llm, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "")
    with pytest.raises(RuntimeError) as ei:
        memory_llm.get_memory_llm()
    assert "LLM_API_KEY" in str(ei.value)
