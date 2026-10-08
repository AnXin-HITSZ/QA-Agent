"""重排序客户端:阿里云百炼 text-rerank(`RERANK_MODEL`,默认 qwen3.7-text-rerank)。

请求结构按百炼原生形态(不是 OpenAI 兼容那套,也不能把模型名换到别的接口上):

    POST {RERANK_API_URL}                      # .../api/v1/services/rerank/text-rerank/text-rerank
    Authorization: Bearer {RERANK_API_KEY}
    {"model": "...", "input": {"query": "...", "documents": ["...", "..."]},
     "parameters": {"top_n": N[, "instruct": "..."]}}

响应:`output.results[{index, relevance_score}]`(按分数降序),另有 `usage.total_tokens`、
`request_id`。有的网关把结果放在**顶层** `results`(Cohere 风格扁平形态),所以解析两处都认;
但**只用 index 映射回记忆 id,绝不用返回的正文定位**(§8)。

几条硬约束:
- 拿不到配置就直接说「未启用」,不假装重排过(调用方据此记降级);
- 网络 / HTTP / 解析失败一律返回带 error 的结果,由检索层回退到 RRF 融合并记录降级
  —— 不把失败当成功,也不把「输入超限」悄悄当成排过序;
- 每次真实 HTTP 请求记一条调用事件(§13):用量取供应商报的 total_tokens,拿不到就置空。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from decimal import Decimal

from app.config import get_settings
from app.metering import build_call, current_context, new_id, record_call
from app.metering.model import (
    BILLING_BILLABLE, BILLING_UNKNOWN, NOTE_FAILED_BILLING, NOTE_NO_USAGE, SERVICE_RERANK,
    STATUS_FAILURE, STATUS_SUCCESS, USAGE_SOURCE_UNKNOWN, USAGE_SOURCE_VENDOR,
)
from app.metering.probe import provider_of
from app.metering.redact import safe_endpoint, safe_error, safe_text

logger = logging.getLogger(__name__)

PRICE_UNIT = "1k_tokens"        # 按千 token 计价(与 embedding 同口径;价目表按此单位配置)
MAX_DOCUMENTS = 500             # 官方上限:单请求最多 500 篇待排文档
HTTP_TIMEOUT_MIN = 1.0


@dataclass(frozen=True)
class RerankOutcome:
    """一次重排的结果:成功时 order 是输入下标(按模型给的顺序),失败时 error 非空。"""

    order: list[int] = field(default_factory=list)
    scores: dict[int, float] = field(default_factory=dict)
    usage_tokens: int | None = None
    request_id: str = ""
    ignored: int = 0            # 被忽略的非法条目(越界 / 重复 / 无分数)
    error: str = ""             # 非空 = 这次没用上模型的结果(调用方必须记降级)

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.order)


def configured() -> bool:
    """是否配置齐了(开关 + 地址 + Key + 模型名)。缺一项都不算可用。"""
    s = get_settings()
    return bool(s.rerank_enabled and (s.rerank_api_url or "").strip()
                and (s.rerank_api_key or "").strip() and (s.rerank_model or "").strip())


def config_note() -> str:
    s = get_settings()
    if not s.rerank_enabled:
        return "重排序已关闭(RERANK_ENABLED=false),本次用 RRF 融合结果"
    return ("未配置重排序(RERANK_API_URL / RERANK_API_KEY 至少缺一项),"
            "本次用 RRF 融合结果")


def rerank(query: str, documents: list[str], *, top_n: int | None = None,
           instruct: str = "") -> RerankOutcome:
    """调一次重排序;网络 / HTTP / 解析问题都返回带 error 的结果(不抛给业务)。"""
    if not documents:
        return RerankOutcome()
    if len(documents) > MAX_DOCUMENTS:
        # 编程错误:调用方应当先按上限裁剪(见 search.py),这里绝不静默截断假装成功
        raise ValueError(f"重排候选 {len(documents)} 篇超过供应商上限 {MAX_DOCUMENTS} 篇")

    s = get_settings()
    if not configured():
        raise RuntimeError("重排序未配置(先判断 rerank.configured())")

    parameters: dict = {}
    if top_n is not None:
        parameters["top_n"] = max(1, min(int(top_n), len(documents)))
    if instruct:
        # 不同任务(回答问题 / 维护决策)用不同排序说明;留空则用供应商默认(问答检索)
        parameters["instruct"] = instruct
    body = {"model": s.rerank_model,
            "input": {"query": query, "documents": list(documents)},
            "parameters": parameters}

    ctx = current_context()
    call_group = new_id()
    started = time.monotonic()
    try:
        payload, status_code = _post(s.rerank_api_url, s.rerank_api_key, body,
                                     timeout=max(HTTP_TIMEOUT_MIN,
                                                 float(s.rerank_timeout_seconds)))
    except Exception as exc:  # noqa: BLE001 —— 超时 / 连接失败:如实记一笔并降级
        error_class, message = safe_error(exc)
        _record(ctx, call_group, started, exc=exc, status_code=None, usage=None,
                request_id="", note="超时或连接失败,已回退到 RRF")
        logger.warning("重排序调用失败(%s):%s", error_class, message)
        return RerankOutcome(error=f"{error_class}:{message}")

    if status_code >= 400:
        code = _error_code(payload)
        detail = f"HTTP {status_code}" + (f"({code})" if code else "")
        _record(ctx, call_group, started, exc=None, status_code=status_code, usage=None,
                request_id="", note=f"供应商返回错误 {detail},已回退到 RRF", failed=True)
        logger.warning("重排序被拒绝:%s", detail)
        return RerankOutcome(error=detail)

    usage = _usage_tokens(payload)
    request_id = str(payload.get("request_id") or "") if isinstance(payload, dict) else ""
    outcome = _parse(payload, count=len(documents))
    if outcome.error:
        _record(ctx, call_group, started, exc=None, status_code=status_code, usage=usage,
                request_id=request_id, note=f"响应不可用({outcome.error}),已回退到 RRF",
                ignored=outcome.ignored)
        logger.warning("重排序响应不可用:%s(忽略 %d 条)", outcome.error, outcome.ignored)
        return RerankOutcome(usage_tokens=usage, request_id=request_id, ignored=outcome.ignored,
                            error=outcome.error)

    _record(ctx, call_group, started, exc=None, status_code=status_code, usage=usage,
            request_id=request_id, note="", ignored=outcome.ignored)
    return RerankOutcome(order=outcome.order, scores=outcome.scores, usage_tokens=usage,
                         request_id=request_id, ignored=outcome.ignored)


# ---- HTTP ----


def _post(url: str, key: str, body: dict, *, timeout: float) -> tuple[dict, int]:
    """发一次请求,返回 (JSON 响应, 状态码);非 JSON 响应体一律按空 dict 处理。"""
    import httpx

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        response = client.post(url, json=body, headers=headers)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
    return payload if isinstance(payload, dict) else {}, int(response.status_code)


def _error_code(payload: dict) -> str:
    """只取错误码,绝不把响应体(可能回显正文)写进日志 / 调用记录。"""
    for path in (("code",), ("error", "code"), ("output", "code")):
        node: object = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, (str, int)):
            return safe_text(node, limit=64)
    return ""


def _usage_tokens(payload: dict) -> int | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    for key in ("total_tokens", "input_tokens", "prompt_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


# ---- 解析与校验 ----


def _parse(payload: dict, *, count: int) -> RerankOutcome:
    """把响应映射成「输入下标 + 分数」;越界 / 重复 / 无分数的条目忽略并计数。"""
    items = payload.get("output", {}).get("results") if isinstance(payload.get("output"), dict) else None
    if items is None:
        items = payload.get("results")              # 扁平(Cohere 风格)形态
    if not isinstance(items, list) or not items:
        return RerankOutcome(error="响应里没有 results")

    order: list[int] = []
    scores: dict[int, float] = {}
    ignored = 0
    for entry in items:
        if not isinstance(entry, dict):
            ignored += 1
            continue
        index = entry.get("index")
        score = entry.get("relevance_score", entry.get("score"))
        if isinstance(index, bool) or not isinstance(index, (int, float)):
            ignored += 1
            continue
        index = int(index)
        if not 0 <= index < count or index in scores:
            ignored += 1                              # 越界 / 重复:宁可少一条,不猜
            continue
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
            ignored += 1
            continue
        scores[index] = float(score)
        order.append(index)
    if not order:
        return RerankOutcome(ignored=ignored, error="响应里没有可用的 index/relevance_score")
    return RerankOutcome(order=order, scores=scores, ignored=ignored)


# ---- 调用日志(§13):每次真实 HTTP 请求一条 ----


def _record(ctx, call_group: str, started: float, *, exc: BaseException | None,
            status_code: int | None, usage: int | None, request_id: str, note: str,
            ignored: int = 0, failed: bool = False) -> None:
    try:
        s = get_settings()
        notes = []
        if usage is None:
            notes.append(NOTE_NO_USAGE)
        if exc is not None:
            notes.append(NOTE_FAILED_BILLING)
        if note:
            notes.append(note)
        if ignored:
            notes.append(f"忽略 {ignored} 条非法排序条目")
        error_class, error_message = safe_error(exc) if exc is not None else ("", "")
        record_call(build_call(
            service=SERVICE_RERANK, provider=provider_of(s.rerank_api_url), target=s.rerank_model,
            endpoint=safe_endpoint(s.rerank_api_url),
            duration_ms=int((time.monotonic() - started) * 1000),
            status=STATUS_FAILURE if (exc is not None or failed) else STATUS_SUCCESS,
            usage_quantity=None if usage is None else Decimal(usage), usage_unit="token",
            usage_source=USAGE_SOURCE_VENDOR if usage is not None else USAGE_SOURCE_UNKNOWN,
            usage_note=";".join(notes),
            billing_status=BILLING_UNKNOWN if (exc is not None or failed) else BILLING_BILLABLE,
            billing_note=NOTE_FAILED_BILLING if (exc is not None or failed) else "",
            price_unit=PRICE_UNIT, error_class=error_class, error_message=error_message,
            http_status=status_code, provider_request_id=request_id or None,
            call_group=call_group, attempt_no=1, http_attempts=1,   # 不自动重试:重试会重复计费
            ctx=ctx,
        ))
    except Exception:  # noqa: BLE001 —— 记账失败绝不影响检索
        logger.warning("重排序调用记录失败(不影响本次检索)", exc_info=True)
