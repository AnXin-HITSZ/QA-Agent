"""文件身份与缓存清单:登记、改名、发布后清理(旧 − 新)、删除与失败恢复。

缓存用进程内 MemoryCache,本地目录用 tmp_path —— 不碰真实 Redis / OSS。
核心断言是方案 §6 / §8:身份稳定、清理只删「旧清单键 − 新清单键」、失败可重试且不重跑 OCR。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.rag import cache_store, documents
from app.rag.cache_store import CacheUnavailable
from app.rag.documents import build_manifest, cache_keys_of
from app.rag.localfs import atomic_write_json


@pytest.fixture
def env(cache_env, monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "index_state_dir", str(tmp_path / "ocr"))
    monkeypatch.setattr(s, "oss_bucket", "test-bucket")
    monkeypatch.setattr(s, "oss_prefix", "knowledge/")
    return SimpleNamespace(cache=cache_env, settings=s, tmp=tmp_path)


def _manifest(doc_id: str, *, key: str = "a.pdf", version: str = "v1",
              pages: list[tuple[int, str | None, str | None]] | None = None,
              embedding_keys: list[str] | None = None) -> dict:
    return build_manifest(document_id=doc_id, oss_key=key, content_sha256="sha",
                          extraction_mode="general", mixed_invoice=False, index_version=version,
                          pages=pages if pages is not None else [(1, "ocr-old", "text-old")],
                          embedding_keys=embedding_keys if embedding_keys is not None else [])


# ---- 登记(§6)----

def test_register_returns_stable_id(env):
    doc_id = documents.register("a.pdf", content_sha256="sha-1")
    assert doc_id and documents.get_id("a.pdf") == doc_id
    assert documents.register("a.pdf", content_sha256="sha-1") == doc_id     # 再登记不换 ID
    record = documents.load_json(env.cache.get(documents.record_key(doc_id)))
    assert record["oss_key"] == "a.pdf" and record["status"] == "active"


def test_content_update_keeps_id(env):
    """经应用更新文件内容:保持同一个 ID,只更新记录(§6)。"""
    doc_id = documents.register("a.pdf", content_sha256="sha-1")
    again = documents.register("a.pdf", content_sha256="sha-2")
    assert again == doc_id
    record = documents.load_json(env.cache.get(documents.record_key(doc_id)))
    assert record["content_sha256"] == "sha-2"


def test_register_normalizes_key_but_keeps_case(env):
    doc_id = documents.register("/d/A.pdf")
    assert documents.get_id("d/A.pdf") == doc_id
    assert documents.get_id("d/a.pdf") is None      # OSS key 区分大小写


def test_path_key_is_namespace_scoped(env, monkeypatch):
    """路径摘要含桶与根前缀:换桶 / 换前缀不会把两个环境的身份混在一起(§6)。"""
    before = documents.path_digest("a.pdf")
    monkeypatch.setattr(env.settings, "oss_bucket", "another-bucket")
    assert documents.path_digest("a.pdf") != before


def test_concurrent_register_has_single_winner(env):
    """同一路径并发登记:SET NX 决定唯一赢家,两边拿到同一个 ID(§6)。"""
    ids: list[str] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        ids.append(documents.register("same.pdf", content_sha256="sha"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(ids) == 2 and len(set(ids)) == 1
    assert documents.get_id("same.pdf") == ids[0]


def test_move_keeps_id_and_caches(env):
    """经应用改名 / 移动:ID 与缓存全部保留,只换路径映射(§6)。"""
    doc_id = documents.register("old/a.pdf", content_sha256="sha")
    documents.write_manifest(_manifest(doc_id, key="old/a.pdf"))
    moved = documents.move("old/a.pdf", "new/b.pdf")
    assert moved == doc_id
    assert documents.get_id("old/a.pdf") is None and documents.get_id("new/b.pdf") == doc_id
    assert documents.manifest_of(doc_id) is not None                 # 缓存清单没丢
    assert documents.load_json(env.cache.get(documents.record_key(doc_id)))["oss_key"] == "new/b.pdf"


def test_cache_disabled_returns_empty(env):
    cache_store.install(cache_store.NullCache())
    assert documents.register("a.pdf") == ""
    assert documents.get_id("a.pdf") is None
    assert documents.forget_file("a.pdf")["skipped"] == "缓存已关闭"


# ---- 清单构造 ----

def test_manifest_skips_pages_without_keys_and_dedups_embeddings():
    m = build_manifest(document_id="d", oss_key="a.pdf", content_sha256="sha",
                       extraction_mode="general", mixed_invoice=False, index_version="v1",
                       pages=[(1, "ocr-1", "text-1"), (2, None, None), (3, "ocr-3", None)],
                       embedding_keys=["emb-1", "emb-1", "", "emb-2"])
    assert [row["page_number"] for row in m["pages"]] == [1, 3]
    assert m["embedding_cache_keys"] == ["emb-1", "emb-2"]
    assert cache_keys_of(m) == {"ocr-1", "text-1", "ocr-3", "emb-1", "emb-2"}
    assert cache_keys_of(None) == set()


# ---- 发布后清理(§8)----

def test_publish_deletes_only_old_minus_new(env):
    cache = env.cache
    doc_id = documents.register("a.pdf", content_sha256="sha")
    documents.write_manifest(_manifest(
        doc_id, version="v1", pages=[(1, "ocr-old", "text-old")],
        embedding_keys=["emb-old", "emb-keep"]))
    for k in ("ocr-old", "text-old", "emb-old", "emb-keep", "另一个文件的键"):
        cache.set(k, "x")

    new = _manifest(doc_id, version="v2", pages=[(1, "ocr-new", "text-new")],
                    embedding_keys=["emb-keep", "emb-new"])
    assert documents.prepare_publish("job-1", "v2", {doc_id: new}) is not None
    out = documents.apply_publish("job-1", active_collection="v2")

    assert out["status"] == "applied" and out["deleted_keys"] == 3
    for k in ("ocr-old", "text-old", "emb-old"):
        assert cache.get(k) is None              # 只在旧清单里 → 删除
    assert cache.get("emb-keep") is not None     # 新旧都有 → 保留
    assert cache.get("另一个文件的键") is not None  # 别的文件的键 → 不动
    assert documents.manifest_of(doc_id)["index_version"] == "v2"   # 正式清单已更新


def test_publish_not_applied_when_version_is_not_active(env):
    """发布没成功(prepared 且不是当前生效版本):正式清单与旧缓存原样保留。"""
    cache = env.cache
    doc_id = documents.register("a.pdf")
    documents.write_manifest(_manifest(doc_id, version="v1", pages=[(1, "ocr-old", None)]))
    cache.set("ocr-old", "x")
    documents.prepare_publish("job-2", "v2", {doc_id: _manifest(doc_id, version="v2")})

    out = documents.apply_publish("job-2", active_collection="v1")
    assert out["status"] == "prepared" and out["skipped"]
    assert cache.get("ocr-old") is not None
    assert documents.manifest_of(doc_id)["index_version"] == "v1"


def test_publish_cleanup_failure_recoverable_without_recompute(env, monkeypatch):
    """删键失败:正式清单已更新、旧键暂留,重试只补删 —— 不重跑 OCR(§8)。"""
    cache = env.cache
    doc_id = documents.register("a.pdf")
    documents.write_manifest(_manifest(doc_id, version="v1", pages=[(1, "ocr-old", None)]))
    cache.set("ocr-old", "x")
    documents.prepare_publish("job-3", "v2", {doc_id: _manifest(
        doc_id, version="v2", pages=[(1, "ocr-new", "text-new")])})

    real_delete = cache.delete
    calls = {"n": 0}

    def flaky_delete(keys):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CacheUnavailable("连接失败")
        return real_delete(keys)

    monkeypatch.setattr(cache, "delete", flaky_delete)
    with pytest.raises(CacheUnavailable):
        documents.apply_publish("job-3", active_collection="v2")
    assert cache.get("ocr-old") is not None                     # 旧键还在
    assert documents.manifest_of(doc_id)["index_version"] == "v2"   # 清单已更新

    assert documents.retry_pending_publishes("v2") == 1          # 启动对账补删
    assert cache.get("ocr-old") is None


def test_stale_publish_recovery_does_not_overwrite_newer_manifest(env, monkeypatch):
    """过期任务恢复不得覆盖较新清单(§8)。

    复现:A 发布 v1 后清理失败(记录停在 publishing),B 又成功发布 v2;
    重试 A 时若按记录里的旧清单回写,正式清单会被写回 v1、并删掉 v2 在用的键。
    """
    cache = env.cache
    doc_id = documents.register("a.pdf")
    documents.write_manifest(_manifest(doc_id, version="v0", pages=[(1, "ocr-v0", None)]))
    cache.set("ocr-v0", "x")

    documents.prepare_publish(
        "job-A", "v1", {doc_id: _manifest(doc_id, version="v1", pages=[(1, "ocr-v1", None)])})
    real_delete = cache.delete
    calls = {"n": 0}

    def flaky_delete(keys):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CacheUnavailable("连接失败")
        return real_delete(keys)

    monkeypatch.setattr(cache, "delete", flaky_delete)
    with pytest.raises(CacheUnavailable):
        documents.apply_publish("job-A", active_collection="v1")
    assert documents.manifest_of(doc_id)["index_version"] == "v1"     # A 的清单已写,键没删成

    monkeypatch.setattr(cache, "delete", real_delete)
    cache.set("ocr-v2", "y")
    documents.prepare_publish(
        "job-B", "v2", {doc_id: _manifest(doc_id, version="v2", pages=[(1, "ocr-v2", None)])})
    assert documents.apply_publish("job-B", active_collection="v2")["status"] == "applied"
    assert documents.manifest_of(doc_id)["index_version"] == "v2"
    assert cache.get("ocr-v1") is None and cache.get("ocr-v2") is not None

    documents.retry_pending_publishes("v2")                            # 重试过期的 A

    assert documents.manifest_of(doc_id)["index_version"] == "v2"      # 不得写回 v1
    assert cache.get("ocr-v2") is not None                             # v2 在用的键不得删
    assert cache.get("ocr-v0") is None                                 # A 遗留的旧键可以补删


def test_stale_publish_recovery_completes_untouched_files(env, monkeypatch):
    """后续发布只改了别的文件:过期任务仍要补完自己那几个文件的清单与清理(§8)。"""
    cache = env.cache
    x = documents.register("x.pdf")
    y = documents.register("y.pdf")
    z = documents.register("z.pdf")
    for doc, key in ((x, "x.pdf"), (y, "y.pdf"), (z, "z.pdf")):
        documents.write_manifest(_manifest(doc, key=key, version="v0",
                                           pages=[(1, f"ocr-{key}-v0", None)]))
        cache.set(f"ocr-{key}-v0", "x")
    documents.prepare_publish("job-A", "v1", {
        x: _manifest(x, key="x.pdf", version="v1", pages=[(1, "ocr-x.pdf-v1", None)]),
        y: _manifest(y, key="y.pdf", version="v1", pages=[(1, "ocr-y.pdf-v1", None)]),
    })
    real_delete = cache.delete

    def failing(keys):                                                 # A 的清理全失败
        raise CacheUnavailable("连接失败")

    monkeypatch.setattr(cache, "delete", failing)
    with pytest.raises(CacheUnavailable):
        documents.apply_publish("job-A", active_collection="v1")
    monkeypatch.setattr(cache, "delete", real_delete)

    # B 只发布了 z.pdf(全库 active 变成 v2,A 的 v1 已不是全库当前版本)
    cache.set("ocr-x.pdf-v1", "x")
    cache.set("ocr-y.pdf-v1", "x")
    cache.set("ocr-z-v2", "z")
    documents.prepare_publish(
        "job-B", "v2", {z: _manifest(z, key="z.pdf", version="v2", pages=[(1, "ocr-z-v2", None)])})
    documents.apply_publish("job-B", active_collection="v2")

    assert documents.retry_pending_publishes("v2") == 1                # A 仍要收尾

    assert documents.manifest_of(x)["index_version"] == "v1"           # 没被跳过的两个文件补完
    assert documents.manifest_of(y)["index_version"] == "v1"
    assert cache.get("ocr-x.pdf-v0") is None and cache.get("ocr-y.pdf-v0") is None
    assert cache.get("ocr-x.pdf-v1") is not None and cache.get("ocr-y.pdf-v1") is not None
    assert documents.manifest_of(z)["index_version"] == "v2"           # B 的文件不受影响
    assert cache.get("ocr-z-v2") is not None


def test_publish_recovery_does_not_resurrect_deleted_file(env):
    """文件在发布与恢复之间被删除(清单已清):恢复不得把文件清单写回来。"""
    cache = env.cache
    doc_id = documents.register("a.pdf")
    documents.write_manifest(_manifest(doc_id, version="v0", pages=[(1, "ocr-v0", None)]))
    cache.set("ocr-v0", "x")
    documents.prepare_publish(
        "job-A", "v1", {doc_id: _manifest(doc_id, version="v1", pages=[(1, "ocr-v1", None)])})
    cache.delete([documents.manifest_key(doc_id)])                     # 文件被删除并清理

    documents.retry_pending_publishes("v1")

    assert documents.manifest_of(doc_id) is None                       # 不复活
    assert cache.get("ocr-v0") is not None                             # 也不猜测删键


def test_publish_recovery_is_repeatable(env, monkeypatch):
    """同一记录的重复恢复:第一次补删,第二次空操作,清单与删除结果不变(§8)。"""
    cache = env.cache
    doc_id = documents.register("a.pdf")
    documents.write_manifest(_manifest(doc_id, version="v0", pages=[(1, "ocr-old", None)]))
    cache.set("ocr-old", "x")
    documents.prepare_publish(
        "job-1", "v1", {doc_id: _manifest(doc_id, version="v1", pages=[(1, "ocr-new", None)])})
    real_delete = cache.delete
    calls = {"n": 0}

    def flaky(keys):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CacheUnavailable("连接失败")
        return real_delete(keys)

    monkeypatch.setattr(cache, "delete", flaky)
    with pytest.raises(CacheUnavailable):
        documents.apply_publish("job-1", active_collection="v1")

    assert documents.retry_pending_publishes("v1") == 1                # 第一次:补删收尾
    assert cache.get("ocr-old") is None
    assert documents.retry_pending_publishes("v1") == 0                # 第二次:记录已 applied
    assert documents.manifest_of(doc_id)["index_version"] == "v1"


def test_retry_pending_publishes_ignores_never_published(env):
    doc_id = documents.register("a.pdf")
    documents.prepare_publish("job-4", "v9", {doc_id: _manifest(doc_id, version="v9")})
    assert documents.retry_pending_publishes("v1") == 0          # 版本对不上 → 不动它


# ---- 删除文件后的清理(§8)----

def test_forget_file_clears_cache_manifest_and_registry(env):
    cache = env.cache
    doc_id = documents.register("d/a.pdf", content_sha256="sha")
    documents.write_manifest(_manifest(doc_id, key="d/a.pdf", pages=[(1, "ocr-k", "text-k")],
                                       embedding_keys=["emb-k"]))
    for k in ("ocr-k", "text-k", "emb-k", "无关的键"):
        cache.set(k, "x")

    out = documents.forget_file("d/a.pdf")
    assert out["document_id"] == doc_id and out["deleted_keys"] == 3
    assert cache.get("无关的键") is not None
    assert documents.manifest_of(doc_id) is None
    assert documents.get_id("d/a.pdf") is None
    assert cache.get(documents.record_key(doc_id)) is None
    assert not documents.deletion_record_path(doc_id).exists()   # 恢复记录执行完即删


def test_reupload_after_delete_gets_new_id(env):
    """删除后重新上传是新文件:新 UUID,不复用旧身份与旧缓存(§6)。"""
    first = documents.register("a.pdf", content_sha256="sha")
    documents.forget_file("a.pdf")
    second = documents.register("a.pdf", content_sha256="sha")
    assert second and second != first


def test_forget_file_without_manifest_deletes_registry_only(env):
    """没清单(从未发布过):只删登记,不猜缓存范围。"""
    doc_id = documents.register("never-indexed.pdf")
    out = documents.forget_file("never-indexed.pdf")
    assert out["document_id"] == doc_id and out["deleted_keys"] == 0
    assert documents.get_id("never-indexed.pdf") is None


def test_forget_file_unknown_key_is_noop(env):
    assert documents.forget_file("nope.pdf")["skipped"] == "未登记"


def test_retry_pending_deletions_without_manifest_does_not_guess(env):
    """记录里确实没有清单(旧格式 / 从未发布):只清登记,不猜缓存删除范围(§8)。"""
    cache = env.cache
    doc_id = documents.register("a.pdf")
    documents.write_manifest(_manifest(doc_id, pages=[(1, "ocr-k", "text-k")]))
    cache.set("ocr-k", "x")
    atomic_write_json(documents.deletion_record_path(doc_id),
                      {"schema_version": 1, "document_id": doc_id, "oss_key": "a.pdf",
                       "manifest": None})                        # 删除时没有清单快照

    assert documents.retry_pending_deletions() == 1
    assert cache.get("ocr-k") is not None                        # 不猜范围 → 键保留
    assert documents.manifest_of(doc_id) is not None             # 没快照可比对 → 清单也保留
    assert documents.get_id("a.pdf") is None                     # 登记与路径映射照常清理
    assert not documents.deletion_record_path(doc_id).exists()
