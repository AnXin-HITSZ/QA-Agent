"""调用日志 / 费用统计的测试道具:构造事件、读回结果、假供应商。

与 ocrkit / pdfkit 同类:只服务测试,不进应用代码。假供应商的职责是**在真实边界上
打探针**:MeteredEmbeddings 靠 httpx 响应钩子取用量,所以假内层客户端每模拟一次
HTTP 响应就调一次 probe.hook —— 与真实链路一致(不触网)。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from langchain_core.embeddings import Embeddings

from app.metering import build_call, new_id
from app.metering.context import PURPOSE_INDEX
from app.metering.model import (
    LAYER_EMBEDDING, SERVICE_EMBEDDING, SERVICE_OCR, STATUS_SUCCESS, USAGE_SOURCE_VENDOR,
    CallItem, CacheEvent,
)
from app.metering.probe import UsageProbe
from app.metering.store import CallFilter

UTC = timezone.utc


# ---- 读回 ----

def calls(store, flt: CallFilter | None = None) -> list[dict]:
    """按时间倒序取回全部调用记录(测试量小,一次取够)。"""
    return store.list_calls(flt or CallFilter(), offset=0, limit=200)["items"]


def cached(store, layer: str | None = None) -> list[dict]:
    """取回缓存统计记录(可按层过滤)。"""
    rows = list(store.cache_events.values())
    return [r for r in rows if layer is None or r["layer"] == layer]


def sum_field(rows: list[dict], field: str) -> Decimal:
    total = Decimal("0")
    for r in rows:
        if r.get(field) is not None:
            total += Decimal(str(r[field]))
    return total


def install_prices(env, *rows: dict) -> None:
    """往内存仓库装价目并刷新价格表(测试里"运维已配好价"的等价物)。"""
    for row in rows:
        env.store.add_price(row)
    env.writer.price_book.rules(refresh=True)


def price_row(service: str, unit_price: str, *, unit: str = "1k_tokens",
              provider: str = "dashscope", target: str = "", days_ago: int = 1,
              currency: str = "CNY") -> dict:
    return {
        "service": service, "provider": provider, "target": target, "unit": unit,
        "currency": currency, "unit_price": Decimal(unit_price),
        "effective_from": (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(),
        "source": "测试价目", "note": "",
    }


# ---- 构造事件 ----

def embedding_call(**over) -> object:
    kwargs = dict(service=SERVICE_EMBEDDING, provider="dashscope", target="text-embedding-v4",
                  endpoint="dashscope.aliyuncs.com", duration_ms=100, status=STATUS_SUCCESS,
                  usage_quantity=Decimal(1000), usage_unit="token",
                  usage_source=USAGE_SOURCE_VENDOR, price_unit="1k_tokens")
    kwargs.update(over)
    return build_call(**kwargs)


def ocr_call(**over) -> object:
    kwargs = dict(service=SERVICE_OCR, provider="aliyun", target="Invoice",
                  endpoint="ocr.aliyuncs.com", duration_ms=300, status=STATUS_SUCCESS,
                  usage_quantity=Decimal(1), usage_unit="request", price_unit="request",
                  items=(CallItem(oss_key="a.pdf", page_no=1, text_count=1),))
    kwargs.update(over)
    return build_call(**kwargs)


def cache_event(**over) -> CacheEvent:
    kwargs = dict(event_id=new_id(), layer=LAYER_EMBEDDING, purpose=PURPOSE_INDEX, unit="text")
    kwargs.update(over)
    return CacheEvent(**kwargs)


def seed(store, calls=(), caches=()) -> None:
    """直接写进仓库(**绕过队列**):只测读路径 / 接口时用,免得每个用例都得先冲刷。"""
    store.insert(list(calls), list(caches))


# ---- 假 Embeddings 供应商 ----

class FakeResponse:
    """httpx.Response 的最小替身:探针只用到 status_code 与 json()。"""

    def __init__(self, payload: dict | None, status: int = 200) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("响应不是 JSON")
        return self._payload


class FakeInnerEmbeddings(Embeddings):
    """假内层客户端:按「收到几次响应」打探针,返回确定性向量。

    tokens=None 模拟「供应商响应里没有 usage」;responses=2 模拟 SDK 内部重试
    (一个收集窗口里看到两次响应);responses=0 + error 模拟超时(一个响应都没有)。
    """

    def __init__(self, probe: UsageProbe, *, dim: int = 4, tokens: int | None = 1000,
                 responses: int = 1, status: int = 200,
                 error: BaseException | None = None) -> None:
        self.probe = probe
        self.dim = dim
        self.tokens = tokens
        self.responses = max(0, responses)
        self.status = status
        self.error = error
        self.calls: list[list[str]] = []       # 每次真正提交的文本(断言"没重复付费调用")

    @property
    def texts(self) -> list[str]:
        return [t for batch in self.calls for t in batch]

    def _respond(self) -> None:
        for _ in range(self.responses):
            body = {} if self.tokens is None else {"usage": {"prompt_tokens": self.tokens}}
            self.probe.hook(FakeResponse(body, self.status))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        self._respond()
        if self.error is not None:
            raise self.error
        return [[float(len(t))] * self.dim for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def metered(inner: Embeddings, probe: UsageProbe, **over):
    """按生产同一参数装配 MeteredEmbeddings(默认值对齐 embeddings.py)。"""
    from app.rag.metered_embeddings import MeteredEmbeddings

    kwargs = dict(provider="dashscope", target="text-embedding-v4",
                  endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1")
    kwargs.update(over)
    return MeteredEmbeddings(inner, probe, **kwargs)
