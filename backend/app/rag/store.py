"""RAG 向量库:Qdrant 薄封装(连接 / 建 collection / upsert / 检索)。

只连真实 Qdrant,连接靠 .env 的 QDRANT_URL / QDRANT_API_KEY;未配置 QDRANT_URL
直接抛错,不留内存兜底(保持生产代码干净)。向量化在 rag/embeddings.py,这里只吃向量。
"""

from __future__ import annotations

from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    ScoredPoint,
    VectorParams,
)

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


def recreate_collection() -> str:
    """清空并重建 collection(丢弃全部旧向量);用于全量重建索引(reindex v1)。返回名称。"""
    s = get_settings()
    client = get_client()
    name = s.qdrant_collection
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=s.embeddings_dim, distance=Distance.COSINE),
    )
    return name


def make_point(point_id: str | int, vector: list[float], payload: dict) -> PointStruct:
    """构造一个向量点(把 Qdrant 的 PointStruct 细节收在本模块内,摄取层不直接依赖 qdrant)。"""
    return PointStruct(id=point_id, vector=vector, payload=payload)


def upsert(points: list[PointStruct]) -> int:
    """写入 / 覆盖向量点,返回写入条数。"""
    if not points:
        return 0
    client = get_client()
    name = ensure_collection()
    client.upsert(collection_name=name, points=points)
    return len(points)


def delete_by_oss_key(key: str) -> int:
    """删除某原件的全部向量点(按 payload oss_key 精确匹配)。返回删除前匹配到的点数。

    用于增量维护:删原件时连带清索引、重新索引前先清旧点(幂等)。collection 不存在
    时视作 0(best-effort:Qdrant 没接通也不该让"删原件"报错)。
    """
    client = get_client()
    name = get_settings().qdrant_collection
    if not client.collection_exists(name):
        return 0
    flt = Filter(must=[FieldCondition(key="oss_key", match=MatchValue(value=key))])
    n = client.count(collection_name=name, count_filter=flt).count
    if n:
        client.delete(collection_name=name, points_selector=FilterSelector(filter=flt))
    return n


def indexed_keys(prefix: str = "") -> set[str]:
    """返回某分类节点(payload category == prefix)下已建立索引的原件 key 集合。

    prefix 为知识库相对前缀(根为空串,其余以 / 结尾),与摄取写入的 category 对齐,
    故只覆盖该节点直属文件(不含子节点)—— 正好供前端逐层浏览时显示已/未索引徽标。
    collection 不存在时返回空集。
    """
    client = get_client()
    name = get_settings().qdrant_collection
    if not client.collection_exists(name):
        return set()
    flt = Filter(must=[FieldCondition(key="category", match=MatchValue(value=prefix))])
    keys: set[str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=name,
            scroll_filter=flt,
            with_payload=["oss_key"],
            with_vectors=False,
            limit=256,
            offset=offset,
        )
        for p in points:
            k = (p.payload or {}).get("oss_key")
            if k:
                keys.add(k)
        if offset is None:
            break
    return keys


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
