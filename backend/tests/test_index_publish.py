"""索引的版本发布与失败保护:发布 / 不发布 / 回退 / 范围外内容。

用真实 Qdrant 的内存实现(QdrantClient(":memory:"))+ 内存 OSS 假仓库:
分块、向量化、写入、指针切换都是真跑,只有 OCR 与 Embeddings 是假的。
"""

from __future__ import annotations

import uuid

import pytest

from app.rag import documents, ingest, ocr, ocr_cache, store
from app.rag.ingest import Options, Scope
from tests import ocrkit
from tests.pdfkit import make_pdf


def _text_pdf(text: str = "quarterly report body") -> bytes:
    return make_pdf(pages=[{"text": text}])


def _forbid_ocr(monkeypatch) -> None:
    """断言某模式下不该碰 OCR:真被调用直接判测试失败(pytest.fail 不被页面级 except 吞掉)。"""
    from app.rag import document_extract as de

    monkeypatch.setattr(de, "recognize_page", lambda *a, **k: pytest.fail("该模式下不允许调用 OCR"))


def _all_points(collection: str) -> list:
    return store.get_client().scroll(collection_name=collection, with_payload=True, limit=1000)[0]


def _keys(collection: str) -> set[str]:
    return {p.payload["oss_key"] for p in _all_points(collection)}


# ---- 整库发布 ----

def test_full_rebuild_publishes_new_version_and_keeps_old(kb_env):
    kb_env.kb.files["01_报告/annual.pdf"] = _text_pdf("annual report 2026")
    before = store.active_collection()

    summary = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options())

    assert summary["published"] is True
    assert summary["indexed_files"] == 1
    assert summary["previous"] == before
    assert store.active_collection() == summary["index_version"]
    assert store.collection_exists(before)              # 旧集合保留,可回退
    assert store.collection_count(summary["index_version"]) == summary["vectors"]


def test_chunk_payload_keeps_source_and_page_numbers(kb_env):
    kb_env.kb.files["01_报告/scan.pdf"] = make_pdf(pages=[{"text": "first page"}, {"text": "second page"}])
    summary = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options())
    points = _all_points(summary["index_version"])
    assert points, "应写入向量"
    p = points[0].payload
    assert p["oss_key"] == "01_报告/scan.pdf"
    assert p["source"] == "scan.pdf"
    assert p["category"] == "01_报告/"
    assert sorted({n for q in points for n in q.payload["page_numbers"]}) == [1, 2]
    assert p["extraction_methods"] == "native"
    assert p["extraction_version"].startswith("v")
    assert p["index_version"] == summary["index_version"]
    assert p["content_sha256"] and p["recognized_fields"] is False


def test_search_reads_active_version(kb_env):
    kb_env.kb.files["a.pdf"] = _text_pdf("needle in the haystack")
    ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options())
    vec = kb_env.embeddings.embed_documents(["needle in the haystack"])[0]
    hits = store.search(vec, top_k=3)
    assert hits and hits[0].payload["oss_key"] == "a.pdf"


# ---- 范围外内容 / 残留 ----

def test_scoped_rebuild_copies_out_of_scope_and_drops_leftovers(kb_env):
    kb_env.kb.files["A/one.pdf"] = _text_pdf("alpha " * 400)     # 旧版本:多块
    kb_env.kb.files["B/two.pdf"] = _text_pdf("beta content")
    first = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options())
    v1 = first["index_version"]
    old_a = len({p.payload["chunk_index"] for p in _all_points(v1) if p.payload["oss_key"] == "A/one.pdf"})
    assert old_a > 1

    kb_env.kb.files["A/one.pdf"] = _text_pdf("alpha")            # 改短:旧的多余切块必须消失
    second = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix="A/"), Options())
    assert second["published"] is True and second["copied_out_of_scope"] > 0

    points = _all_points(second["index_version"])
    by_key: dict[str, list] = {}
    for p in points:
        by_key.setdefault(p.payload["oss_key"], []).append(p)
    assert set(by_key) == {"A/one.pdf", "B/two.pdf"}              # 范围外内容保留
    assert len(by_key["A/one.pdf"]) == 1                          # 范围内整份重建,无残留
    assert by_key["B/two.pdf"][0].payload["index_version"] == v1  # 范围外点原样复制


def test_deleted_out_of_scope_file_is_not_resurrected(kb_env):
    kb_env.kb.files["A/keep.pdf"] = _text_pdf("keep me")
    kb_env.kb.files["B/gone.pdf"] = _text_pdf("delete me")
    ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options())

    def progress(done, total, detail):
        if done == 1:
            kb_env.kb.delete_object("B/gone.pdf")   # 准备期间范围外文件被删

    kb_env.kb.files["A/keep.pdf"] = _text_pdf("keep me updated")
    summary = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix="A/"),
                                   Options(), progress=progress)
    assert summary["published"] is True
    assert _keys(summary["index_version"]) == {"A/keep.pdf"}


# ---- 失败保护 ----

def test_failed_file_blocks_publish_and_old_index_survives(kb_env, monkeypatch):
    """页面级失败(OCR 超时)属于"该有内容没抽全":不发布,旧索引照常可检索。"""
    kb_env.kb.files["scan.pdf"] = make_pdf(pages=[{"scan": True}])
    kb_env.kb.files["good.pdf"] = _text_pdf("stable content")
    ocrkit.install(monkeypatch, text="识别结果")
    first = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""),
                                 Options(extraction_mode="general"))
    assert first["published"] is True
    active_v1 = store.active_collection()

    ocrkit.install(monkeypatch, boom=ocr.OCRError("Throttling", "限流"))
    kb_env.kb.files["good.pdf"] = _text_pdf("changed content")
    second = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""),
                                  Options(extraction_mode="general"))

    assert second["published"] is False
    assert second["failed_files"] == 1
    assert store.active_collection() == active_v1                  # 指针没动
    assert not store.collection_exists(second["index_version"])    # 候选集合已清理
    assert _keys(active_v1) == {"scan.pdf", "good.pdf"}            # 旧索引照常可检索
    assert any("Throttling" in (d.get("reason") or "") for d in second["files"])


def test_unreadable_file_is_skipped_not_blocking(kb_env):
    """坏文件 / 不支持的格式只算跳过:如实记录原因,不拖住整库发布。"""
    kb_env.kb.files["good.pdf"] = _text_pdf("fine")
    kb_env.kb.files["bad.pdf"] = b"%PDF-1.4 this is not really a pdf"
    kb_env.kb.files["old.doc"] = b"\xd0\xcf\x11\xe0binary doc"
    summary = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options())
    assert summary["published"] is True
    assert summary["skipped_files"] == 2
    assert {s["key"] for s in summary["skipped"]} == {"bad.pdf", "old.doc"}


def test_ocr_page_failure_blocks_publish(kb_env, monkeypatch):
    kb_env.kb.files["scan.pdf"] = make_pdf(pages=[{"scan": True}])
    ocrkit.install(monkeypatch, boom=ocr.OCRError("Throttling", "限流"))
    summary = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options(extraction_mode="general"))
    assert summary["published"] is False
    assert summary["failed_files"] == 1
    assert any("Throttling" in (d.get("reason") or "") for d in summary["files"])
    assert store.read_manifest().get("active") is None      # 从未发布过,指针仍是空的


def test_source_changed_during_prepare_blocks_publish(kb_env):
    kb_env.kb.files["a.pdf"] = _text_pdf("version one")
    ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options())
    v1 = store.active_collection()

    def progress(done, total, detail):
        kb_env.kb.touch("a.pdf")          # 准备完之后源文件被改写

    summary = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options(), progress=progress)
    assert summary["published"] is False
    assert "重新排队" in summary["message"]
    assert store.active_collection() == v1


def test_needs_ocr_file_is_skipped_not_failed(kb_env, monkeypatch):
    """native_only 下扫描件只是"跳过",不该拖垮整批发布(旧索引照常换新)。"""
    kb_env.kb.files["a.pdf"] = _text_pdf("normal")
    kb_env.kb.files["scan.pdf"] = make_pdf(pages=[{"scan": True}])
    _forbid_ocr(monkeypatch)

    summary = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options())
    assert summary["published"] is True
    assert summary["skipped_files"] == 1
    assert "needs_ocr" in summary["skipped"][0]["reason"]
    assert _keys(summary["index_version"]) == {"a.pdf"}


# ---- OCR 方式 ----

def test_ocr_mode_indexes_every_page_with_page_numbers(kb_env, monkeypatch):
    kb_env.kb.files["scan.pdf"] = make_pdf(pages=[{"text": "digital page"}, {"scan": True}, {"scan": True}])
    seen: list[tuple[int, str]] = []
    ocrkit.install(monkeypatch, text="识别出的正文", fields={"发票号码": "123"}, calls=seen)

    summary = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""),
                                   Options(extraction_mode="invoice"))
    assert summary["published"] is True
    # 第 1 页(本来有文本层)也按用户的显式选择送识别;第 2/3 页渲染出的字节相同 →
    # 第 3 页命中第 2 页的识别缓存,共享一次付费调用(§4.1 页码不进键)。
    assert [c[1] for c in seen] == ["invoice", "invoice"]
    points = _all_points(summary["index_version"])
    assert sorted({n for p in points for n in p.payload["page_numbers"]}) == [1, 2, 3]
    assert all(p.payload["extraction_methods"] == "ocr" for p in points)
    assert all(p.payload["recognized_fields"] is True for p in points)


def test_mixed_invoice_option_uses_mixed_type(kb_env, monkeypatch):
    kb_env.kb.files["tickets.pdf"] = make_pdf(pages=[{"scan": True}])
    seen: list[tuple[int, str]] = []
    ocrkit.install(monkeypatch, text="票据", calls=seen)

    ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""),
                         Options(extraction_mode="invoice", mixed_invoice=True))
    assert [c[1] for c in seen] == ["mixed_invoice"]


def test_refresh_ocr_option_recalls_supplier(kb_env, monkeypatch):
    kb_env.kb.files["s.pdf"] = make_pdf(pages=[{"scan": True}])
    calls: list[tuple[int, str]] = []
    ocrkit.install(monkeypatch, text="正文", calls=calls)

    opts = Options(extraction_mode="general")
    ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), opts)
    ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), opts)
    assert len(calls) == 1                     # 第二次命中缓存,不重复付费

    ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""),
                         Options(extraction_mode="general", refresh_ocr=True))
    assert len(calls) == 2


# ---- 缓存清单与发布后清理(§7 / §8)----

def test_publish_records_manifest_and_reuses_embedding_cache(kb_env):
    kb_env.kb.files["d/a.pdf"] = _text_pdf("stable chunk text")
    first = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options())
    assert first["published"] is True
    assert first["cache"]["status"] == "applied" and first["cache"]["files"] == 1

    doc_id = documents.get_id("d/a.pdf")
    manifest = documents.manifest_of(doc_id)
    assert manifest["index_version"] == first["index_version"]
    assert manifest["oss_key"] == "d/a.pdf" and manifest["content_sha256"]
    assert manifest["embedding_cache_keys"]                  # 切块向量键已登记

    batches = kb_env.embeddings.batches
    second = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options())
    assert second["published"] is True
    assert kb_env.embeddings.batches == batches              # 相同切块复用向量,没重复付费


def test_ocr_manifest_records_page_cache_keys(kb_env, monkeypatch):
    kb_env.kb.files["s.pdf"] = make_pdf(pages=[{"scan": True}])
    ocrkit.install(monkeypatch, text="识别正文")

    summary = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""),
                                   Options(extraction_mode="general"))
    assert summary["published"] is True
    manifest = documents.manifest_of(documents.get_id("s.pdf"))
    assert manifest["extraction_mode"] == "general"
    assert [row["page_number"] for row in manifest["pages"]] == [1]
    row = manifest["pages"][0]
    assert row["ocr_cache_key"].startswith(f"qa:cache:ocr:{ocr_cache.KEY_VERSION}:")
    assert row["text_cache_key"].startswith(f"qa:cache:text:{ocr_cache.KEY_VERSION}:")


def test_reindex_after_content_change_cleans_old_cache_keys(kb_env, monkeypatch):
    """原件内容更新:身份保持同一个 ID,发布后按「旧 − 新」清掉不再使用的缓存键。"""
    kb_env.kb.files["s.pdf"] = make_pdf(pages=[{"scan": True}])
    ocrkit.install(monkeypatch, text="第一版正文")
    first = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""),
                                 Options(extraction_mode="general"))
    assert first["published"] is True
    doc_id = documents.get_id("s.pdf")
    old_keys = documents.cache_keys_of(documents.manifest_of(doc_id))
    assert old_keys and all(kb_env.cache.get(k) is not None for k in old_keys)

    kb_env.kb.files["s.pdf"] = _text_pdf("updated body")     # 渲染字节变了 → 新键
    ocrkit.install(monkeypatch, text="第二版正文")
    second = ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""),
                                  Options(extraction_mode="general"))
    assert second["published"] is True
    assert documents.get_id("s.pdf") == doc_id                # 内容更新不换身份(§6)

    new_keys = documents.cache_keys_of(documents.manifest_of(doc_id))
    assert new_keys != old_keys
    for k in old_keys - new_keys:
        assert kb_env.cache.get(k) is None                    # 旧缓存已按清单差集清理
    for k in new_keys:
        assert kb_env.cache.get(k) is not None                # 新清单里的键都还在


# ---- 单文件索引(keys 范围)/ 回退 ----

def test_single_file_index_is_idempotent(kb_env):
    kb_env.kb.files["one.pdf"] = _text_pdf("single file")
    first = ingest.run_index_job(kb_env.kb, Scope(kind="keys", keys=("one.pdf",)), Options())
    assert first["published"] is True and first["chunks"] > 0
    v1 = store.active_collection()

    second = ingest.run_index_job(kb_env.kb, Scope(kind="keys", keys=("one.pdf",)), Options())
    assert second["published"] is True
    assert store.active_collection() != v1
    assert len(_all_points(store.active_collection())) == second["chunks"]   # 不重复累积


def test_single_file_missing_key_reports_reason(kb_env):
    out = ingest.run_index_job(kb_env.kb, Scope(kind="keys", keys=("nope.pdf",)), Options())
    assert out["indexed_files"] == 0
    assert "not_found" in out["files"][0]["reason"]


def test_rollback_switches_pointer_back(kb_env):
    kb_env.kb.files["a.pdf"] = _text_pdf("v1 content")
    ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options())
    v1 = store.active_collection()
    kb_env.kb.files["a.pdf"] = _text_pdf("v2 content")
    ingest.run_index_job(kb_env.kb, Scope(kind="prefix", prefix=""), Options())
    v2 = store.active_collection()
    assert v2 != v1

    store.rollback()
    m = store.read_manifest()
    assert store.active_collection() == v1
    assert m["previous"] == v2          # 回退后,被换下来的版本成为回退目标
    assert m["history"][-1]["action"] == "rollback"
    assert _keys(v1) == {"a.pdf"}


def test_rollback_without_previous_raises(kb_env):
    with pytest.raises(RuntimeError):
        store.rollback()


def test_publish_refuses_when_count_mismatches(kb_env):
    kb_env.kb.files["a.pdf"] = _text_pdf("content")
    embeddings = kb_env.embeddings
    version = store.create_staging()
    point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "test-point"))
    store.upsert([store.make_point(point_id, embeddings.embed_documents(["t"])[0], {"oss_key": "a.pdf"})],
                 collection=version)
    with pytest.raises(RuntimeError):
        store.publish(version, expected_count=99)
    assert store.read_manifest().get("active") is None


def test_legacy_collection_stays_untouched_until_first_publish(kb_env):
    """迁移前状态:没有 manifest 时,读写仍指向 .env 里的物理集合名。"""
    assert store.active_collection() == kb_env.settings.qdrant_collection
    assert store.read_manifest() == {}
