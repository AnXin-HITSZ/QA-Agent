"""RAG 向量库:Qdrant 薄封装(连接 / 建 collection / upsert / 检索)。

只连真实 Qdrant,连接靠 .env 的 QDRANT_URL / QDRANT_API_KEY;未配置 QDRANT_URL
直接抛错,不留内存兜底(保持生产代码干净)。向量化在 rag/embeddings.py,这里只吃向量。
"""

from __future__ import annotations

from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, ScoredPoint, VectorParams

from app.config import get_settings


@lru_cache
def get_client() -> QdrantClient:
    s = get_settings()
    if not s.qdrant_url:
        raise RuntimeError(
            "未配置 QDRANT_URL:请在 backend/.env 指向你的 Qdrant"
            "(如 http://<ECS-IP>:6333)后再使用 RAG。"
        )
    return QdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key or None)


def ensure_collection() -> str:
    """确保 collection 存在(向量维度取 EMBEDDINGS_DIM,距离 cosine);返回名称。"""
    s = get_settings()
    client = get_client()
    name = s.qdrant_collection
    if not client.collection_exists(name):
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=s.embeddings_dim, distance=Distance.COSINE),
        )
    return name


def upsert(points: list[PointStruct]) -> int:
    """写入 / 覆盖向量点,返回写入条数。"""
    if not points:
        return 0
    client = get_client()
    name = ensure_collection()
    client.upsert(collection_name=name, points=points)
    return len(points)


def search(query_vector: list[float], top_k: int = 5) -> list[ScoredPoint]:
    """按向量检索,返回命中(含 payload 与 score),按相关度降序。"""
    client = get_client()
    name = ensure_collection()
    result = client.query_points(
        collection_name=name,
        query=query_vector,
        limit=max(1, top_k),
        with_payload=True,
    )
    return result.points
