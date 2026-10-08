"""带计量的聊天模型包装:一次 invoke() = 一条调用事件(长期记忆 §13「调用日志与费用」)。

为什么需要它:
- 长期记忆的提取与维护决策要走现有调用统计(用途 / 模型 / 真实重试 / 用量 / 费用估算 /
  降级),但**聊天主链路仍然不计量**(见 .env.example 的计量节)—— 于是把计量放在
  记忆自己的 LLM 客户端上,而不是改 app/llm.py 的共享实例;
- 一次 invoke 可能被 SDK 内部重试(同一个 httpx 客户端):http_attempts 用响应探针
  观测到的真实请求次数如实记录,不假装只有一次;
- 用量取供应商响应报的 usage(langchain 归一化到 AIMessage.usage_metadata;拿不到时
  退回探针从原始响应里读过的字段),**绝不按字数猜 token**;拿不到就置空并写明原因;
- 失败(超时 / 5xx / 限流)也记一条:供应商是否计费未知,置 unknown、不记 0 —— 超时
  不等于免费;
- 记账失败绝不影响业务:记录异常只告警,业务调用该成功就成功,不把一次成功的付费调用
  变成需要重新付费的业务重试。

关闭计量时整层直接透传,与未引入本模块时行为一致。
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from app.metering import build_call, current_context, new_id, record_call
from app.metering.model import (
    BILLING_BILLABLE, BILLING_UNKNOWN, NOTE_FAILED_BILLING, NOTE_NO_USAGE, NOTE_SDK_RETRY,
    SERVICE_LLM, STATUS_FAILURE, STATUS_SUCCESS, USAGE_SOURCE_UNKNOWN, USAGE_SOURCE_VENDOR,
)
from app.metering.probe import UsageProbe, attempts, chat_tokens
from app.metering.redact import safe_endpoint, safe_error

logger = logging.getLogger(__name__)

# 聊天模型的计费单位:与价目表里的 llm 行一致(每 1k token,用量取输入 + 输出合计)。
PRICE_UNIT = "1k_tokens"


def _usage_from_message(message) -> tuple[int | None, int | None, int | None] | None:
    """langchain 归一化的 AIMessage.usage_metadata → (输入, 输出, 合计)。"""
    meta = getattr(message, "usage_metadata", None)
    if not isinstance(meta, dict):
        return None
    inp, out, total = meta.get("input_tokens"), meta.get("output_tokens"), meta.get("total_tokens")
    inp = int(inp) if isinstance(inp, (int, float)) else None
    out = int(out) if isinstance(out, (int, float)) else None
    total = int(total) if isinstance(total, (int, float)) else None
    if total is None and (inp is not None or out is not None):
        total = (inp or 0) + (out or 0)
    if total is None:
        return None
    return inp, out, total


class MeteredChat:
    """聊天模型包装:逐次调用计时 + 取用量 + 记事件,业务行为与内层完全一致。"""

    def __init__(self, inner, probe: UsageProbe | None = None, *, provider: str, target: str,
                 endpoint: str = "") -> None:
        self._inner = inner
        self._probe = probe or UsageProbe()
        self._provider = provider
        self._target = target
        self._endpoint = safe_endpoint(endpoint)

    # ---- 对外接口(与 BaseChatModel.invoke 一致,只包同步调用) ----

    def invoke(self, messages, **kwargs):
        if not self._metering_on():
            return self._inner.invoke(messages, **kwargs)
        ctx = current_context()
        call_group = new_id()
        started = time.monotonic()
        with self._probe.collecting() as seen:
            try:
                message = self._inner.invoke(messages, **kwargs)
            except BaseException as exc:  # noqa: BLE001 —— 失败也要记一笔,再原样抛给业务
                self._record(ctx, call_group, started, seen, exc=exc, message=None)
                raise
        self._record(ctx, call_group, started, seen, exc=None, message=message)
        return message

    # ---- 一次真实调用 ----

    def _metering_on(self) -> bool:
        try:
            from app.metering import active
            return active()
        except Exception:  # noqa: BLE001 —— 计量判断失败一律按关闭处理
            return False

    def _record(self, ctx, call_group: str, started: float, seen: list[dict], *,
                exc: BaseException | None, message) -> None:
        try:
            duration_ms = int((time.monotonic() - started) * 1000)
            tried = attempts(seen)
            http_status = int(seen[-1]["status"]) if seen else None
            error_class, error_message = safe_error(exc) if exc is not None else ("", "")

            tokens = _usage_from_message(message) if message is not None else None
            if tokens is None:
                # 兜底:响应探针从原始响应里读过的 usage(失败时响应若真报了用量,照记事实;
                # 是否计费仍由 billing_status 单独如实标记)
                tokens = chat_tokens(seen)
            inp, out, total = tokens if tokens is not None else (None, None, None)

            notes = []
            if total is None:
                notes.append(NOTE_NO_USAGE)
            if tried > 1:
                notes.append(NOTE_SDK_RETRY)
            if exc is not None:
                notes.append(NOTE_FAILED_BILLING)
            if total is not None:
                # 如实写明用量的构成:价目表按合计 token 配单价(输入 / 输出不同价时按综合单价)
                parts = []
                if inp is not None:
                    parts.append(f"输入 {inp}")
                if out is not None:
                    parts.append(f"输出 {out}")
                notes.append(f"供应商报的{'+'.join(parts)} token,合计 {total}"
                             if parts else f"供应商报的合计 {total} token")

            event = build_call(
                service=SERVICE_LLM, provider=self._provider, target=self._target,
                endpoint=self._endpoint, duration_ms=duration_ms,
                status=STATUS_FAILURE if exc is not None else STATUS_SUCCESS,
                usage_quantity=None if total is None else Decimal(total),
                usage_unit="token",
                usage_source=USAGE_SOURCE_VENDOR if total is not None else USAGE_SOURCE_UNKNOWN,
                usage_note=";".join(notes),
                billing_status=BILLING_BILLABLE if exc is None else BILLING_UNKNOWN,
                billing_note="" if exc is None else NOTE_FAILED_BILLING,
                price_unit=PRICE_UNIT,
                error_class=error_class, error_message=error_message, http_status=http_status,
                call_group=call_group, attempt_no=1, http_attempts=max(1, tried),
                ctx=ctx,
            )
            record_call(event)
        except Exception:  # noqa: BLE001 —— 记账失败绝不影响业务
            logger.warning("LLM 调用记录失败(不影响本次调用结果)", exc_info=True)
