"""RAG 向量化:从 .env 建 OpenAI 兼容的 embeddings(需真实 key)。

仿 app/llm.py 的 get_llm:未配 EMBEDDINGS_API_KEY 直接抛错,不做假兜底。
阿里云 DashScope 走兼容模式端点即可(text-embedding-v3,默认 1024 维)。
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

    return OpenAIEmbeddings(
        base_url=s.embeddings_base_url,
        api_key=s.embeddings_api_key,
        model=s.embeddings_model,
        # 第三方(非 OpenAI)端点:关掉基于 tiktoken 的按 token 分批,按原文发送,避免误判。
        check_embedding_ctx_length=False,
        # DashScope text-embedding-v4 单请求 ≤10 条输入 → 按 10 一批发送(embed_documents 内部分批)。
        chunk_size=10,
    )
