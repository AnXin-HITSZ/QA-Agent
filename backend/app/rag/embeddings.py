"""RAG 向量化:从 .env 建 OpenAI 兼容的 embeddings(需真实 key)。

仿 app/llm.py 的 get_llm:未配 EMBEDDINGS_API_KEY 直接抛错,不做假兜底。
阿里云 DashScope 走兼容模式端点即可(qwen3.7-text-embedding-flash,默认 1024 维)。
"""

from __future__ import annotations

from functools import lru_cache

from langchain_core.embeddings import Embeddings

from app.config import get_settings


@lru_cache
def get_embeddings() -> Embeddings:
    s = get_settings()

    if not s.embeddings_api_key:
        raise RuntimeError(
            "未配置 EMBEDDINGS_API_KEY:RAG 需要一个 OpenAI 兼容的 embeddings 端点"
            "(如阿里云 DashScope),请在 backend/.env 设置后再使用。"
        )

    # 任意 OpenAI 兼容的 embeddings 端点(阿里云 DashScope / 硅基流动 / 智谱 ...)。
    from langchain_openai import OpenAIEmbeddings

    # 计量:用量探针(响应钩子)+ 包装层。惰性 import,未用到向量化时不加载。
    from app.metering.probe import UsageProbe
    from app.rag.metered_embeddings import MeteredEmbeddings

    probe = UsageProbe()          # 与下面的 httpx 客户端同生命周期(闭包持有,不会被回收)

    inner = OpenAIEmbeddings(
        base_url=s.embeddings_base_url,
        api_key=s.embeddings_api_key,
        model=s.embeddings_model,
        # 第三方(非 OpenAI)端点:关掉基于 tiktoken 的按 token 分批,按原文发送,避免误判。
        check_embedding_ctx_length=False,
        # 单请求条数上限:qwen3.7-text-embedding-flash 为 20(官方模型表「最大行数」)。
        # 这里保守留 10:请求更小、失败重试的影响面更小;要压请求数可放宽到 20,
        # 记得与 metered_embeddings.EMBED_BATCH 同步改。
        chunk_size=10,
        # 自带 httpx 客户端:响应钩子在那里取供应商报的 usage(调用日志与费用统计用)。
        # 默认 HTTP 超时仍由 SDK 逐请求设置,行为与未传 http_client 时一致。
        http_client=_probe_client(probe),
    )

    # 计量包装:每次真实请求记一条调用事件;关闭计量时逐字透传(见 metered_embeddings)。
    return MeteredEmbeddings(inner, probe, provider=_provider_of(s.embeddings_base_url),
                             target=s.embeddings_model, endpoint=s.embeddings_base_url)


def _probe_client(probe):
    """建带用量钩子的 httpx 客户端(惰性 import,未用 embeddings 时不加载 httpx)。"""
    import httpx

    return httpx.Client(event_hooks={"response": [probe.hook]}, follow_redirects=True)


def _provider_of(base_url: str) -> str:
    """供应商标签取端点主机名:价目表按它配置,与「实际打到哪个网关」一致。"""
    from urllib.parse import urlsplit

    host = urlsplit(base_url or "").hostname or ""
    return host or "openai-compatible"
