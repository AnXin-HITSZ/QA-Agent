"""embedding 缓存(第 3 层):命中复用、批量去重、锁超时报忙碌、维度校验、配置变更自然失效。

假客户端只做计数与确定性向量,缓存那一层真实执行(进程内 MemoryCache)。
查询侧(检索)不经过本模块的缓存 —— 见 tests/test_retrieve.py。
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from app.config import get_settings
from app.rag import cache_store, embedding_cache
from app.rag.cache_store import CacheBusy


class CountingEmbeddings:
    """确定性假向量 + 调用计数:batch 次数与 query 次数用来断言"没重复付费"。"""

    def __init__(self, dim: int, *, bad: str = "") -> None:
        self.dim = dim
        self.bad = bad                  # "short" / "nan":返回非法向量
        self.doc_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    def _vec(self, text: str) -> list[float]:
        if self.bad == "short":
            return [0.5] * (self.dim - 1)
        if self.bad == "nan":
            return [float("nan")] * self.dim
        return [float((hash(text) % 89) + 1) / 100.0] * self.dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.doc_calls.append(list(texts))
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return self._vec(text)


@pytest.fixture
def client(cache_env):
    return CountingEmbeddings(get_settings().embeddings_dim)


# ---- 切块向量 ----

def test_documents_second_call_hits_cache(client):
    first = embedding_cache.embed_documents(client, ["同一段正文"])
    second = embedding_cache.embed_documents(client, ["同一段正文"])
    assert first == second
    assert len(client.doc_calls) == 1                   # 第二次整批命中,没再提交


def test_identical_chunks_from_different_files_share_vector(client):
    embedding_cache.embed_documents(client, ["共同片段", "文件 A 的片段"])
    embedding_cache.embed_documents(client, ["文件 B 的片段", "共同片段"])
    assert [call for call in client.doc_calls] == [["共同片段", "文件 A 的片段"],
                                                   ["文件 B 的片段"]]   # 只提交新片段


def test_partial_hit_only_submits_missing_texts(client):
    embedding_cache.embed_documents(client, ["甲"])
    out = embedding_cache.embed_documents(client, ["甲", "乙"])
    assert len(out) == 2
    assert client.doc_calls[-1] == ["乙"]               # 已缓存的"甲"不重复提交


def test_cache_key_does_not_include_source_path(client):
    """来源路径不进键:同一段文本在不同文件里复用同一个向量缓存键(§4.3)。"""
    key = embedding_cache.cache_key("正文")
    assert "a.pdf" not in key and embedding_cache.cache_key("正文") == key


def test_payload_has_no_source_fields(cache_env):
    client = CountingEmbeddings(get_settings().embeddings_dim)
    embedding_cache.embed_documents(client, ["正文"])
    payload = json.loads(cache_env.get(embedding_cache.cache_key("正文")))
    assert payload["vector"] and payload["dim"] == get_settings().embeddings_dim
    assert not {"oss_key", "source", "category", "chunk_index"} & set(payload)


# ---- 批量去重与并发(§4.3 / §5)----

def test_duplicate_texts_in_one_batch_are_submitted_once(client):
    """同一批里的重复文本只提交一次,结果在各位置复用(不重复付费)。"""
    out = embedding_cache.embed_documents(client, ["重复段", "别的", "重复段"])
    assert client.doc_calls == [["重复段", "别的"]]      # 去重后只提交两条
    assert out[0] == out[2] and len(out) == 3


def test_busy_instead_of_computing_without_lock(cache_env, monkeypatch):
    """别人持锁且等不到:抛 CacheBusy,本进程一次都没提交(绝不绕过锁付费,§5)。"""
    monkeypatch.setattr(get_settings(), "cache_lock_wait_seconds", 0.1)
    client = CountingEmbeddings(get_settings().embeddings_dim)
    cache_env.acquire_lock(
        cache_store.lock_name("embedding", embedding_cache.embedding_digest("文本")), ttl=30)

    with pytest.raises(CacheBusy):
        embedding_cache.embed_documents(client, ["文本"])
    assert client.doc_calls == []
    assert cache_env.get(embedding_cache.cache_key("文本")) is None


def test_waits_for_other_process_then_reuses_result(cache_env, monkeypatch):
    """没抢到锁:等对方写缓存,等到了就复用,自己不再提交。"""
    monkeypatch.setattr(get_settings(), "cache_lock_wait_seconds", 2.0)
    lkey = cache_store.lock_name("embedding", embedding_cache.embedding_digest("共用文本"))
    token = cache_env.acquire_lock(lkey, ttl=30)
    dim = get_settings().embeddings_dim
    client = CountingEmbeddings(dim)

    def _other_process():
        time.sleep(0.05)
        cache_env.set(embedding_cache.cache_key("共用文本"),
                      json.dumps({"vector": [0.25] * dim}))     # 对方算好写缓存
        cache_env.release_lock(lkey, token)

    threading.Thread(target=_other_process, daemon=True).start()
    out = embedding_cache.embed_documents(client, ["共用文本"])

    assert out == [[0.25] * dim]
    assert client.doc_calls == []


# ---- 校验:非法值不写缓存,坏缓存按未命中 ----

def test_wrong_dimension_result_is_not_written_and_raises(cache_env):
    bad = CountingEmbeddings(get_settings().embeddings_dim, bad="short")
    with pytest.raises(RuntimeError):
        embedding_cache.embed_documents(bad, ["文本"])
    assert cache_env.get(embedding_cache.cache_key("文本")) is None


def test_non_finite_result_is_not_written_and_raises(cache_env):
    bad = CountingEmbeddings(get_settings().embeddings_dim, bad="nan")
    with pytest.raises(RuntimeError):
        embedding_cache.embed_documents(bad, ["文本"])
    assert cache_env.get(embedding_cache.cache_key("文本")) is None


def test_cached_value_with_wrong_dimension_is_a_miss(cache_env):
    """历史缓存维度与当前配置不符:按未命中重算并覆盖,不把坏向量发去检索。"""
    key = embedding_cache.cache_key("文本")
    cache_env.set(key, json.dumps({"vector": [0.1, 0.2]}))
    client = CountingEmbeddings(get_settings().embeddings_dim)
    vec = embedding_cache.embed_documents(client, ["文本"])[0]
    assert client.doc_calls == [["文本"]] and len(vec) == get_settings().embeddings_dim
    assert len(json.loads(cache_env.get(key))["vector"]) == get_settings().embeddings_dim


def test_corrupt_cached_value_is_a_miss(cache_env):
    key = embedding_cache.cache_key("文本")
    cache_env.set(key, "{不是 JSON")
    client = CountingEmbeddings(get_settings().embeddings_dim)
    embedding_cache.embed_documents(client, ["文本"])
    assert client.doc_calls == [["文本"]]


# ---- 配置变更 → 键自然不同(无需清缓存) ----

def test_model_change_changes_key(cache_env, monkeypatch):
    before = embedding_cache.cache_key("文本")
    monkeypatch.setattr(get_settings(), "embeddings_model", "text-embedding-v9")
    assert embedding_cache.cache_key("文本") != before


def test_version_bump_changes_key(cache_env, monkeypatch):
    before = embedding_cache.cache_key("文本")
    # 在**当前值**上加后缀再比对:写死 "2" 会随本地 .env 的 EMBEDDINGS_VERSION 一起翻车。
    bumped = get_settings().embeddings_version + "-bump"
    monkeypatch.setattr(get_settings(), "embeddings_version", bumped)
    assert embedding_cache.cache_key("文本") != before


def test_dim_change_changes_key(cache_env, monkeypatch):
    before = embedding_cache.cache_key("文本")
    monkeypatch.setattr(get_settings(), "embeddings_dim", 512)
    assert embedding_cache.cache_key("文本") != before


def test_cache_disabled_still_returns_vectors(cache_env):
    """CACHE_BACKEND=none:照常返回向量(每次都重新计算),不报错。"""
    cache_store.install(cache_store.NullCache())
    client = CountingEmbeddings(get_settings().embeddings_dim)
    v1 = embedding_cache.embed_documents(client, ["文本"])
    v2 = embedding_cache.embed_documents(client, ["文本"])
    assert v1 == v2 and len(client.doc_calls) == 2


@pytest.mark.parametrize("failure", ["busy", "redis"])
def test_partial_lock_acquisition_always_releases_owned_locks(cache_env, monkeypatch, client, failure):
    monkeypatch.setattr(get_settings(), "cache_lock_wait_seconds", 0)
    free = cache_store.lock_name("embedding", embedding_cache.embedding_digest("free"))
    busy = cache_store.lock_name("embedding", embedding_cache.embedding_digest("busy"))
    original = cache_env.acquire_lock
    owner = original(busy, 60)
    if failure == "redis":
        def acquire(key, ttl):
            if key == busy:
                raise cache_store.CacheUnavailable("offline")
            return original(key, ttl)
        monkeypatch.setattr(cache_env, "acquire_lock", acquire)
    error = CacheBusy if failure == "busy" else cache_store.CacheUnavailable
    with pytest.raises(error):
        embedding_cache.embed_documents(client, ["free", "busy"])
    assert original(free, 60) is not None
    assert cache_env.renew_lock(busy, owner, 60)
    assert client.doc_calls == []
