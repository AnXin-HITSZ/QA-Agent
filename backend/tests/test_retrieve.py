"""检索链路(§4.3 修订):查询向量不落缓存 —— 已有知识库的检索不依赖 Redis。

用内存 Qdrant + 假 Embeddings 真跑一遍索引与检索,只有 OCR 是假的。
核心断言:缓存断开(或未配置)时检索照常工作,且每个问题都直接调 embedding 客户端。
"""

from __future__ import annotations

import pytest

from app.rag import cache_store, ingest, retrieve
from app.rag.cache_store import CacheUnavailable, MemoryCache
from app.rag.ingest import Options, Scope
from app.rag import embedding_cache
from tests.pdfkit import make_pdf

TEXT = "needle in the haystack"


class _BrokenCache(MemoryCache):
    """Redis 断了:读写与取锁全部抛 CacheUnavailable(绝不是「未命中」)。"""

    def check(self):
        raise CacheUnavailable("Redis 连接失败:测试")

    def get(self, key):
        raise CacheUnavailable("Redis 连接失败:测试")

    def set(self, key, value):
        raise CacheUnavailable("Redis 连接失败:测试")

    def delete(self, keys):
        raise CacheUnavailable("Redis 连接失败:测试")

    def acquire_lock(self, key, ttl):
        raise CacheUnavailable("Redis 连接失败:测试")


@pytest.fixture
def indexed(kb_env, monkeypatch):
    """先正常建好索引(缓存健康),再把检索用的 embeddings 接到同一个假实现上。"""
    kb_env.kb.files["a.pdf"] = make_pdf(pages=[{"text": TEXT}])
    ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options())
    monkeypatch.setattr(retrieve, "get_embeddings", lambda: kb_env.embeddings)
    kb_env.embeddings.texts.clear()
    return kb_env


@pytest.mark.parametrize("broken", [True, False], ids=["redis故障", "未接缓存"])
def test_search_works_without_working_cache(indexed, broken):
    """建好索引后 Redis 挂掉(或本就没接缓存):检索必须照常返回命中。"""
    if broken:
        cache_store.install(_BrokenCache())
    else:
        cache_store.install(cache_store.NullCache())

    hits = retrieve.search_knowledge(TEXT, top_k=3)

    assert hits and hits[0]["oss_key"] == "a.pdf"
    assert hits[0]["source"] == "a.pdf" and hits[0]["text"].strip()


def test_query_vector_is_not_cached(indexed):
    """同一个问题问两次:每次都直接调客户端,**不写入任何 embedding 缓存键**。"""
    query = "报销标准是多少?"
    key = embedding_cache.cache_key(query)
    assert indexed.cache.get(key) is None

    first = retrieve.search_knowledge(query, top_k=3)
    second = retrieve.search_knowledge(query, top_k=3)

    assert indexed.embeddings.texts == [query, query]     # 两次都调了客户端(没走缓存)
    assert indexed.cache.get(key) is None                 # 也没写缓存
    assert [h["oss_key"] for h in first] == [h["oss_key"] for h in second]


def test_search_drops_hits_below_threshold(indexed):
    """无关问题:相似度低于阈值 → 不返回命中(避免把噪声塞给 LLM)。"""
    assert retrieve.search_knowledge("完全无关的问题", score_threshold=0.999) == []
    assert retrieve.search_knowledge("", top_k=3) == []    # 空问题直接短路,不调客户端
