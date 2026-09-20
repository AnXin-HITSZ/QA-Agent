"""RAG 检索:把用户问题向量化 → Qdrant 语义检索 → 结构化命中(供工具与引用来源用)。

v1 纯语义检索,不带分类 / 元数据过滤(全库);低于分数阈值的命中丢弃以降噪。
命中同时服务两个通道:给 LLM 阅读的正文片段,与给 API 层的引用元数据(oss_key →
后续在 API 层签成短时效 URL,不在此处签、也不存库)。
"""

from __future__ import annotations

from app.rag import store
from app.rag.embeddings import get_embeddings

DEFAULT_TOP_K = 5
# 余弦相似度阈值:低于此判为「不相关」丢弃。经验默认,需按真实语料再调
# (text-embedding-v4 余弦下,弱相关常落在 0.2 上下)。
SCORE_THRESHOLD = 0.2
_SNIPPET_CHARS = 300


def search_knowledge(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    score_threshold: float = SCORE_THRESHOLD,
) -> list[dict]:
    """向量检索知识库,返回按相关度降序的命中列表(纯语义、全库、无过滤)。

    每个命中为 dict:text(切块正文)、oss_key、source(文件名)、category(分类前缀)、
    score(余弦相似度)、chunk_index、ext。低于 score_threshold 或正文为空的命中被丢弃。
    未配置 Embeddings / Qdrant 时上抛 RuntimeError,由工具层捕获后优雅降级。
    """
    q = (query or "").strip()
    if not q:
        return []
    vector = get_embeddings().embed_query(q)
    points = store.search(vector, top_k=top_k)
    hits: list[dict] = []
    for p in points:
        if p.score is not None and p.score < score_threshold:
            continue
        payload = p.payload or {}
        text = (payload.get("text") or "").strip()
        if not text:
            continue
        hits.append(
            {
                "text": text,
                "oss_key": payload.get("oss_key"),
                "source": payload.get("source"),
                "category": payload.get("category"),
                "score": p.score,
                "chunk_index": payload.get("chunk_index"),
                "ext": payload.get("ext"),
            }
        )
    return hits


def format_hits(hits: list[dict], snippet_chars: int = _SNIPPET_CHARS) -> str:
    """把命中列表拼成给 LLM 阅读的文本(编号 + 来源文件名 + 片段)。"""
    blocks: list[str] = []
    for i, h in enumerate(hits, start=1):
        snippet = h.get("text") or ""
        if len(snippet) > snippet_chars:
            snippet = snippet[:snippet_chars].rstrip() + "…"
        src = h.get("source") or h.get("oss_key") or "未知来源"
        blocks.append(f"[{i}] 来源:{src}\n{snippet}")
    return "\n\n".join(blocks)
