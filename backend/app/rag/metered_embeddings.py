"""带计量的 embedding 客户端:每次真实 HTTP 请求记一条调用事件(技术方案 §3 §4)。

为什么要包一层:
- langchain-openai 的 embed_documents 只回传向量,响应里的 usage(prompt_tokens)被丢掉,
  而官方用量是估算费用唯一可信的来源 → 用 httpx 响应钩子(probe)在请求边界上取;
- 一次 embed_documents 内部按 chunk_size 拆成多个 HTTP 请求,我们需要**按请求**记账:
  因此这里显式按 10 条一批自己拆(EMBED_BATCH,与 embeddings.py 的 chunk_size 一致),
  一批 = 一次请求 = 一条事件,不虚报也不合并;
- 供应商 SDK 的内部重试:钩子能看到几次响应就记几次(http_attempts),不假装没发生。

关闭计量时整层直接透传(与未引入本模块时逐字节一致),不影响现有调用链。
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from langchain_core.embeddings import Embeddings

from app.metering import build_call, current_context, new_id, record_call
from app.metering.model import (
    BILLING_BILLABLE, BILLING_UNKNOWN, SERVICE_EMBEDDING, STATUS_FAILURE, STATUS_SUCCESS,
    USAGE_SOURCE_UNKNOWN, USAGE_SOURCE_VENDOR, CallItem,
)
from app.metering.probe import UsageProbe, attempts, prompt_tokens
from app.metering.redact import safe_error, safe_endpoint

logger = logging.getLogger(__name__)

# 单请求输入条数上限:qwen3.7-text-embedding-flash 为 20,这里与 embeddings.py 的 chunk_size
# 对齐保守取 10(要放宽到 20 就两边一起改)。
EMBED_BATCH = 10

NOTE_NO_USAGE = "供应商响应未返回 usage,无法取得 token 数(不按字数折算)"
NOTE_SDK_RETRY = "含供应商 SDK 内部重试,http_attempts 为观测到的真实请求次数"
NOTE_FAILED_BILLING = "调用失败:供应商是否计费未知,不按 0 计"


class MeteredEmbeddings(Embeddings):
    """Embeddings 包装:逐请求计时 + 取用量 + 记事件,业务行为与内层完全一致。"""

    def __init__(self, inner: Embeddings, probe: UsageProbe, *, provider: str, target: str,
                 endpoint: str = "", batch: int = EMBED_BATCH) -> None:
        self._inner = inner
        self._probe = probe
        self._provider = provider
        self._target = target
        self._endpoint = safe_endpoint(endpoint)
        self._batch = max(1, int(batch))

    # ---- 对外接口(与 Embeddings 一致) ----

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self._metering_on():
            return self._inner.embed_documents(texts)
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            chunk = texts[i: i + self._batch]
            out.extend(self._request(chunk))
        return out

    def embed_query(self, text: str) -> list[float]:
        if not self._metering_on():
            return self._inner.embed_query(text)
        return self._request([text])[0]

    # ---- 一次真实请求 ----

    def _metering_on(self) -> bool:
        try:
            from app.metering import active
            return active()
        except Exception:  # noqa: BLE001 —— 计量判断失败一律按关闭处理
            return False

    def _request(self, chunk: list[str]) -> list[list[float]]:
        """发一次(逻辑上的)请求并记一条事件;业务异常原样上抛,不吞。"""
        ctx = current_context()
        call_group = new_id()
        started = time.monotonic()
        with self._probe.collecting() as seen:
            try:
                vectors = self._inner.embed_documents(chunk)
            except BaseException as exc:  # noqa: BLE001 —— 失败也要记一笔,再原样抛给业务
                self._record(ctx, call_group, started, seen, exc=exc, text_count=len(chunk))
                raise
        self._record(ctx, call_group, started, seen, exc=None, text_count=len(chunk))
        return vectors

    def _record(self, ctx, call_group: str, started: float, seen: list[dict],
                *, exc: BaseException | None, text_count: int) -> None:
        try:
            duration_ms = int((time.monotonic() - started) * 1000)
            tried = attempts(seen)
            tokens = prompt_tokens(seen)
            http_status = int(seen[-1]["status"]) if seen else None
            error_class, error_message = safe_error(exc) if exc is not None else ("", "")

            notes = []
            if tokens is None:
                notes.append(NOTE_NO_USAGE)
            if tried > 1:
                notes.append(NOTE_SDK_RETRY)
            if exc is not None:
                notes.append(NOTE_FAILED_BILLING)

            event = build_call(
                service=SERVICE_EMBEDDING, provider=self._provider, target=self._target,
                endpoint=self._endpoint, duration_ms=duration_ms,
                status=STATUS_FAILURE if exc is not None else STATUS_SUCCESS,
                usage_quantity=None if tokens is None else Decimal(tokens),
                usage_unit="token",
                usage_source=USAGE_SOURCE_VENDOR if tokens is not None else USAGE_SOURCE_UNKNOWN,
                usage_note=";".join(notes),
                billing_status=BILLING_BILLABLE if exc is None else BILLING_UNKNOWN,
                billing_note="" if exc is None else NOTE_FAILED_BILLING,
                price_unit="1k_tokens",
                error_class=error_class, error_message=error_message, http_status=http_status,
                call_group=call_group, attempt_no=1, http_attempts=max(1, tried),
                items=(CallItem(document_id=ctx.document_id, oss_key=ctx.oss_key,
                                text_count=text_count),),
                ctx=ctx,
            )
            record_call(event)
        except Exception:  # noqa: BLE001 —— 记账失败绝不影响业务
            logger.warning("embedding 调用记录失败(不影响本次向量化)", exc_info=True)
