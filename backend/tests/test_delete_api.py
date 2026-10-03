"""删除文件的恢复闭环(方案 §8):先落记录再删原件、四阶段推进、失败可重试、不误伤重传。

全部在内存 OSS / 内存缓存上跑,不碰真实 OSS、Redis 与 Qdrant。
核心断言:Redis 读不到必要信息时**原件必须还在**;任何中断点都能由启动对账补完;
同路径重新上传拿到新身份后,延迟执行的旧删除不得破坏它。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rag import cache_store, documents, ingest
from app.rag.cache_store import CacheUnavailable, MemoryCache
from app.rag.localfs import atomic_write_json, read_json

API = "/api/v1/knowledge"


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


class _OssBatchDown(Exception):
    """OSS 批量删除失败。刻意不用 RuntimeError:_dep_503 把 RuntimeError 当作「依赖未配置」→503。"""


class _BrokenCache(MemoryCache):
    """Redis 断了:任何读写都抛 CacheUnavailable(绝不是「未命中」)。"""

    def get(self, key):
        raise CacheUnavailable("Redis 连接失败:测试")

    def set(self, key, value):
        raise CacheUnavailable("Redis 连接失败:测试")

    def delete(self, keys):
        raise CacheUnavailable("Redis 连接失败:测试")


def _published(env, key: str = "a.pdf", *, body: bytes = b"body") -> tuple[str, list[str]]:
    """造一个「已登记 + 已发布」的文件:清单与缓存键齐全,返回 (doc_id, 清单里的键)。"""
    env.kb.put_object(key, body)
    doc_id = documents.register(key, content_sha256="sha")
    keys = [f"ocr-{key}", f"text-{key}", f"emb-{key}"]
    documents.write_manifest(documents.build_manifest(
        document_id=doc_id, oss_key=key, content_sha256="sha", extraction_mode="general",
        mixed_invoice=False, index_version="v1",
        pages=[(1, f"ocr-{key}", f"text-{key}")], embedding_keys=[f"emb-{key}"]))
    for k in keys:
        env.cache.set(k, "x")
    return doc_id, keys


def _deletion_records(env) -> list:
    d = env.cache_dir / "deletions"
    return sorted(d.glob("*.json")) if d.is_dir() else []


def _record_of(env, filename) -> dict:
    return read_json(env.cache_dir / "deletions" / filename, default=None)


# ---- 正常删除:四个阶段一次走完 ----

def test_delete_object_cleans_cache_manifest_and_registry(client, kb_env):
    doc_id, keys = _published(kb_env)
    kb_env.cache.set("别的文件的键", "keep")

    assert client.delete(f"{API}/object", params={"key": "a.pdf"}).status_code == 204

    assert "a.pdf" not in kb_env.kb.files
    for k in keys:
        assert kb_env.cache.get(k) is None
    assert kb_env.cache.get("别的文件的键") is not None
    assert documents.manifest_of(doc_id) is None
    assert documents.get_id("a.pdf") is None
    assert _deletion_records(kb_env) == []          # 收尾完成,恢复记录不残留


def test_delete_object_records_before_removing_object(client, kb_env, monkeypatch):
    """恢复记录必须在删原件**之前**落盘(否则 Redis 抖动会留下无凭据的删除)。"""
    doc_id, _ = _published(kb_env)
    seen = {}
    real_delete = kb_env.kb.delete_object

    def spy(key):
        rec = _record_of(kb_env, f"{doc_id}.json")
        seen["record_at_delete"] = rec is not None
        seen["phases"] = (rec or {}).get("phases")
        return real_delete(key)

    monkeypatch.setattr(kb_env.kb, "delete_object", spy)
    assert client.delete(f"{API}/object", params={"key": "a.pdf"}).status_code == 204

    assert seen["record_at_delete"] is True
    assert seen["phases"]["object_deleted"] is False    # 还没删,阶段如实为 False


# ---- Redis 故障:宁可删不掉,也不留缺口 ----

def test_delete_object_keeps_object_when_registry_unreadable(client, kb_env):
    _published(kb_env)
    cache_store.install(_BrokenCache())

    r = client.delete(f"{API}/object", params={"key": "a.pdf"})

    assert r.status_code == 503 and "Redis" in r.json()["detail"]
    assert "a.pdf" in kb_env.kb.files                   # 原件必须还在
    assert _deletion_records(kb_env) == []              # 也没留下"待删除"记录


def test_delete_object_keeps_object_when_manifest_unreadable(client, kb_env, monkeypatch):
    """登记读得到、清单读不到:同样不删原件(清单缺了就不知道该清哪些缓存)。"""
    _published(kb_env)
    real_get = kb_env.cache.get
    target = documents.manifest_key(documents.get_id("a.pdf"))

    def flaky(key):
        if key == target:
            raise CacheUnavailable("Redis 读清单失败:测试")
        return real_get(key)

    monkeypatch.setattr(kb_env.cache, "get", flaky)
    r = client.delete(f"{API}/object", params={"key": "a.pdf"})

    assert r.status_code == 503
    assert "a.pdf" in kb_env.kb.files
    assert _deletion_records(kb_env) == []


def test_delete_object_keeps_object_when_oss_delete_fails(client, kb_env, monkeypatch):
    """OSS 删除失败:撤销删除记录,文件保持完整可用(下次还能正常索引)。"""
    doc_id, _ = _published(kb_env)

    def boom(key):
        raise RuntimeError("OSS 网络不通:测试")

    monkeypatch.setattr(kb_env.kb, "delete_object", boom)
    assert client.delete(f"{API}/object", params={"key": "a.pdf"}).status_code == 503

    assert "a.pdf" in kb_env.kb.files
    assert _deletion_records(kb_env) == []              # 记录已撤销
    assert documents.get_id("a.pdf") == doc_id          # 身份与登记原样保留
    stored = documents.load_json(kb_env.cache.get(documents.record_key(doc_id)))
    assert stored["status"] == "active"                 # deleting 标记已复位


# ---- 中途失败:启动对账补完 ----

def test_delete_object_cleanup_failure_recovered_by_reconcile(client, kb_env, monkeypatch):
    """缓存清理失败:记录留着,重启对账只补没做完的阶段(不重跑 OCR,也不碰原件)。"""
    doc_id, keys = _published(kb_env)
    real_delete = kb_env.cache.delete
    state = {"fail": True}

    def flaky(ks):
        if state["fail"]:
            raise CacheUnavailable("Redis 抖动:测试")
        return real_delete(ks)

    monkeypatch.setattr(kb_env.cache, "delete", flaky)
    assert client.delete(f"{API}/object", params={"key": "a.pdf"}).status_code == 204

    assert "a.pdf" not in kb_env.kb.files               # 原件已删
    assert any(kb_env.cache.get(k) is not None for k in keys)   # 缓存还没清掉
    records = _deletion_records(kb_env)
    assert len(records) == 1
    rec = _record_of(kb_env, records[0].name)
    assert rec["phases"]["object_deleted"] is True
    assert rec["phases"]["cache_cleaned"] is False
    assert rec["cache_keys"] == sorted(keys)            # 待删范围来自删除时的快照

    state["fail"] = False
    assert documents.retry_pending_deletions() == 1

    for k in keys:
        assert kb_env.cache.get(k) is None
    assert documents.manifest_of(doc_id) is None
    assert documents.get_id("a.pdf") is None
    assert _deletion_records(kb_env) == []


def test_delete_qdrant_failure_records_phase_and_recovery_retries(client, kb_env, monkeypatch):
    """Qdrant 失败保留未完成阶段；服务恢复后继续删除。"""
    doc_id, keys = _published(kb_env)
    calls: list[str] = []

    def boom(key):
        calls.append(key)
        raise RuntimeError("Qdrant 连接失败:测试")

    monkeypatch.setattr(ingest, "delete_file_index", boom)
    assert client.delete(f"{API}/object", params={"key": "a.pdf"}).status_code == 204

    assert calls == ["a.pdf"]
    assert documents.get_id("a.pdf") == doc_id
    record = read_json(documents.deletion_record_path(doc_id))
    assert record["phases"]["vectors_deleted"] is False
    assert all(kb_env.cache.get(k) is not None for k in keys)
    monkeypatch.setattr(ingest, "delete_file_index", lambda key: 0)
    assert documents.retry_pending_deletions() == 1
    assert documents.get_id("a.pdf") is None
    assert all(kb_env.cache.get(k) is None for k in keys)
    assert _deletion_records(kb_env) == []


def test_delete_recovery_retries_vector_phase(client, kb_env, monkeypatch):
    """记录里 vectors_deleted=False:恢复时补试一次向量删除。"""
    doc_id, _ = _published(kb_env, key="b.pdf")
    kb_env.kb.delete_object("b.pdf")
    atomic_write_json(documents.deletion_record_path(doc_id), {
        "schema_version": 1, "document_id": doc_id, "oss_key": "b.pdf",
        "manifest": documents.manifest_of(doc_id), "cache_keys": [], "registry": {},
        "phases": {"object_deleted": True, "vectors_deleted": False,
                   "cache_cleaned": False, "registry_cleaned": False}})
    calls: list[str] = []
    monkeypatch.setattr(ingest, "delete_file_index",
                        lambda key: calls.append(key) or 0)

    documents.retry_pending_deletions()

    assert calls == ["b.pdf"]
    assert _deletion_records(kb_env) == []


def test_delete_recovery_with_legacy_record(client, kb_env):
    """旧格式记录(没有 phases):按「原件已删」处理,只补缓存与登记两个阶段。"""
    doc_id, keys = _published(kb_env, key="a.pdf")
    atomic_write_json(documents.deletion_record_path(doc_id), {
        "schema_version": 1, "document_id": doc_id, "oss_key": "a.pdf",
        "manifest": documents.manifest_of(doc_id)})

    assert documents.retry_pending_deletions() == 1

    for k in keys:
        assert kb_env.cache.get(k) is None
    assert documents.get_id("a.pdf") is None
    assert _deletion_records(kb_env) == []


# ---- 同路径重新上传 vs 延迟执行的旧删除 ----

def test_pending_deletion_does_not_break_reupload_at_same_path(client, kb_env, monkeypatch):
    """删除没走完时同路径重传:新文件拿到新身份,旧删除的恢复不得破坏它(§6/§8)。"""
    old_id, old_keys = _published(kb_env, key="a.pdf")
    real_run = documents._run_deletion
    calls = {"n": 0}

    def die_once(path):                                  # 模拟清理阶段进程中断(只断第一次)
        calls["n"] += 1
        if calls["n"] == 1:
            # 模拟向量已删除并记录后，在缓存清理前退出。
            documents.mark_deletion({"document_id": old_id}, vectors_deleted=True)
            raise RuntimeError("进程在清理阶段退出")
        return real_run(path)

    monkeypatch.setattr(documents, "_run_deletion", die_once)
    assert client.delete(f"{API}/object", params={"key": "a.pdf"}).status_code == 204
    assert len(_deletion_records(kb_env)) == 1

    # 用户重新上传同一个路径(上传处理器调的就是 register)
    kb_env.kb.put_object("a.pdf", b"new body")
    new_id = documents.register("a.pdf", content_sha256="sha-new")
    assert new_id and new_id != old_id                   # 新身份,不继承旧 document_id
    documents.write_manifest(documents.build_manifest(
        document_id=new_id, oss_key="a.pdf", content_sha256="sha-new",
        extraction_mode="general", mixed_invoice=False, index_version="v2",
        pages=[(1, "ocr-new", None)], embedding_keys=[]))
    kb_env.cache.set("ocr-new", "y")

    documents.retry_pending_deletions()                  # 延迟执行的旧删除恢复

    assert documents.get_id("a.pdf") == new_id           # 新文件的登记没被破坏
    assert documents.manifest_of(new_id)["index_version"] == "v2"
    assert kb_env.cache.get("ocr-new") is not None       # 新文件在用的键没被删
    assert kb_env.cache.get("ocr-a.pdf") is None         # 旧文件自己的缓存仍按删除清理
    assert _deletion_records(kb_env) == []


# ---- 目录删除 ----

def test_delete_folder_prepares_all_records_before_deleting(client, kb_env, monkeypatch):
    _published(kb_env, key="d/a.pdf")
    _published(kb_env, key="d/b.pdf")
    seen = {}
    real = kb_env.kb.delete_prefix

    def spy(prefix):
        seen["records"] = len(_deletion_records(kb_env))
        return real(prefix)

    monkeypatch.setattr(kb_env.kb, "delete_prefix", spy)
    r = client.delete(f"{API}/folder", params={"prefix": "d/"})

    assert r.status_code == 200 and r.json()["deleted"] == 2
    assert seen["records"] == 2                          # 两个文件的记录都先落了盘
    assert kb_env.kb.files == {}
    assert documents.get_id("d/a.pdf") is None and documents.get_id("d/b.pdf") is None
    assert _deletion_records(kb_env) == []


def test_delete_folder_aborts_records_when_preparation_fails(client, kb_env, monkeypatch):
    """准备阶段中途失败:已落的记录全部撤销,一个原件都不删(§8)。"""
    _published(kb_env, key="d/a.pdf")
    _published(kb_env, key="d/b.pdf")
    real_get = kb_env.cache.get
    bad = documents.path_key("d/b.pdf")

    def flaky(key):
        if key == bad:
            raise CacheUnavailable("Redis 抖动:测试")
        return real_get(key)

    monkeypatch.setattr(kb_env.cache, "get", flaky)
    r = client.delete(f"{API}/folder", params={"prefix": "d/"})

    assert r.status_code == 503
    assert set(kb_env.kb.files) == {"d/a.pdf", "d/b.pdf"}
    assert _deletion_records(kb_env) == []


def test_delete_folder_batch_failure_keeps_records_then_recovers(client, kb_env, monkeypatch):
    """批量删除失败:记录保留(无法知道删到哪了),恢复时先补删原件再清缓存(§8)。"""
    _, keys_a = _published(kb_env, key="d/a.pdf")
    _, keys_b = _published(kb_env, key="d/b.pdf")

    def boom(prefix):
        raise _OssBatchDown("OSS 批量删除失败:测试")

    monkeypatch.setattr(kb_env.kb, "delete_prefix", boom)
    assert client.delete(f"{API}/folder", params={"prefix": "d/"}).status_code == 500

    assert len(_deletion_records(kb_env)) == 2           # 记录都在,原件也还在
    assert set(kb_env.kb.files) == {"d/a.pdf", "d/b.pdf"}

    assert documents.retry_pending_deletions() == 2      # 恢复逐文件补删原件(object_exists 核对)

    assert kb_env.kb.files == {}                         # 恢复补删了原件
    for k in keys_a + keys_b:
        assert kb_env.cache.get(k) is None
    assert documents.get_id("d/a.pdf") is None and documents.get_id("d/b.pdf") is None
    assert _deletion_records(kb_env) == []


# ---- 未登记 / 空 key ----

def test_delete_unregistered_file_still_removes_object(client, kb_env):
    """从未登记过的文件:没有缓存可清,但原件照删(不留记录)。"""
    kb_env.kb.put_object("raw.pdf", b"x")
    assert client.delete(f"{API}/object", params={"key": "raw.pdf"}).status_code == 204
    assert "raw.pdf" not in kb_env.kb.files
    assert _deletion_records(kb_env) == []


def test_delete_folder_requires_prefix(client, kb_env):
    assert client.delete(f"{API}/folder", params={"prefix": "/"}).status_code == 400


@pytest.mark.parametrize("object_deleted", [False, True])
def test_recovery_conflict_preserves_reuploaded_object_and_vectors(kb_env, monkeypatch, object_deleted):
    old_id, _ = _published(kb_env)
    pending = documents.prepare_deletion("a.pdf")
    documents.mark_deletion(pending, object_deleted=object_deleted)
    kb_env.kb.put_object("a.pdf", b"replacement")
    new_id = documents.register("a.pdf", content_sha256="new-hash")
    assert new_id != old_id
    calls = []
    monkeypatch.setattr(ingest, "delete_file_index", lambda key: calls.append(key))
    assert documents.retry_pending_deletions() == 0
    assert kb_env.kb.get_object("a.pdf") == b"replacement"
    assert calls == []
    assert documents.get_id("a.pdf") == new_id
    assert documents.deletion_record_path(old_id).exists()
