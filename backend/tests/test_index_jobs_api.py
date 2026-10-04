"""索引任务 API:202 + 轮询、非法参数 422、并发 409、明细分页、版本指针与回退。

后台线程跑的是真实编排(内存 OSS + 内存 Qdrant + 假 Embeddings),OCR 仍然全部打桩。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rag import ingest, ocr_jobs, store
from tests.pdfkit import make_pdf

ADMIN = "/api/v1/admin/knowledge"


@pytest.fixture
def client():
    # 不用 with TestClient(app):那会跑 lifespan(连远程 Redis 的记忆/待办初始化),
    # 测试只要知识库路由;任务对账另有 test_reconcile_marks_running_job_failed 直接测。
    return TestClient(app)


def _job(scope: dict, **options) -> dict:
    """任务请求体:提取方式必须显式给(方案 §8),测试统一从这里构造。"""
    return {"scope": scope, "options": {"extraction_mode": "native_only", **options}}


def _wait(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    """轮询到任务结束(后台线程,通常几百毫秒内完成)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"{ADMIN}/index-jobs/{job_id}")
        assert r.status_code == 200
        job = r.json()
        if job["status"] in ("published", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"任务 {job_id} 超时未结束")


def test_create_job_runs_and_reports_progress(client, kb_env):
    kb_env.kb.files["01_报告/a.pdf"] = make_pdf(pages=[{"text": "report body"}])
    r = client.post(f"{ADMIN}/index-jobs",
                    json=_job({"kind": "prefix", "prefix": "01_报告/"}))
    assert r.status_code == 202
    job = r.json()
    assert job["status"] in ("queued", "running")
    assert job["job_id"]

    done = _wait(client, job["job_id"])
    assert done["status"] == "published" and done["published"] is True
    assert done["progress"]["done"] == 1 and done["progress"]["total"] == 1
    assert done["summary"]["indexed_files"] == 1

    files = client.get(f"{ADMIN}/index-jobs/{job['job_id']}/files").json()
    assert files["total"] == 1
    assert files["items"][0]["key"] == "01_报告/a.pdf"
    assert files["items"][0]["status"] == "indexed"
    assert files["items"][0]["pages"]["total"] == 1

    cur = client.get(f"{ADMIN}/index-jobs/current").json()
    assert cur["job_id"] == job["job_id"]


def test_job_files_pagination(client, kb_env):
    for i in range(5):
        kb_env.kb.files[f"d/f{i}.pdf"] = make_pdf(pages=[{"text": f"content {i}"}])
    job_id = client.post(f"{ADMIN}/index-jobs",
                         json=_job({"kind": "prefix", "prefix": "d/"})).json()["job_id"]
    _wait(client, job_id)

    page = client.get(f"{ADMIN}/index-jobs/{job_id}/files", params={"offset": 2, "limit": 2}).json()
    assert page["total"] == 5 and len(page["items"]) == 2
    assert [i["key"] for i in page["items"]] == ["d/f2.pdf", "d/f3.pdf"]


def test_job_with_explicit_keys(client, kb_env):
    kb_env.kb.files["a.pdf"] = make_pdf(pages=[{"text": "aaa"}])
    kb_env.kb.files["b.pdf"] = make_pdf(pages=[{"text": "bbb"}])
    job_id = client.post(f"{ADMIN}/index-jobs",
                         json=_job({"kind": "keys", "keys": ["b.pdf"]})).json()["job_id"]
    done = _wait(client, job_id)
    assert done["summary"]["indexed_files"] == 1
    assert store.keys_in(store.active_collection()) == {"b.pdf"}


# ---- 缓存依赖(§9:未接通明确报错,不把失败当未命中) ----

def test_cache_not_configured_blocks_job_with_503(client, kb_env, monkeypatch):
    from app.config import get_settings
    from app.rag import cache_store

    cache_store.install(None)                    # 按配置重建后端
    monkeypatch.setattr(get_settings(), "cache_backend", "redis")
    monkeypatch.setattr(get_settings(), "redis_url", "")
    kb_env.kb.files["a.pdf"] = make_pdf(pages=[{"text": "x"}])

    r = client.post(f"{ADMIN}/index-jobs", json=_job({"kind": "prefix"}))
    assert r.status_code == 503
    assert "缓存未接通" in r.json()["detail"]


def test_cache_disabled_still_indexes(client, kb_env):
    """CACHE_BACKEND=none 是显式选择:索引照常跑,只是没有缓存、清单为空。"""
    from app.rag import cache_store

    cache_store.install(cache_store.NullCache())
    kb_env.kb.files["a.pdf"] = make_pdf(pages=[{"text": "body"}])
    job_id = client.post(f"{ADMIN}/index-jobs", json=_job({"kind": "prefix"})).json()["job_id"]
    done = _wait(client, job_id)
    assert done["status"] == "published" and done["summary"]["indexed_files"] == 1
    assert done["summary"]["cache"]["status"] == "skipped"


def test_cache_oom_pauses_job_with_explicit_message(client, kb_env, monkeypatch):
    """内存上限:任务暂停、如实报出中文文案,不发布(旧索引继续可用,§9)。"""
    from app.rag import cache_store
    from app.rag.cache_store import CacheOutOfMemory
    from tests import ocrkit

    class _Full(cache_store.MemoryCache):
        def set(self, key, value):
            raise CacheOutOfMemory("used memory > 'maxmemory'")

    cache_store.install(_Full())
    kb_env.kb.files["scan.pdf"] = make_pdf(pages=[{"scan": True}])
    ocrkit.install(monkeypatch, text="正文")

    job_id = client.post(f"{ADMIN}/index-jobs",
                         json=_job({"kind": "prefix"}, extraction_mode="general")).json()["job_id"]
    done = _wait(client, job_id)
    assert done["status"] == "failed"
    assert "内存上限" in done["error"] and "本次索引已暂停" in done["error"]
    assert done["summary"]["cache"]["status"] == "failed"
    assert store.read_manifest().get("active") is None       # 未发布,旧索引不受影响


# ---- 非法参数 ----

@pytest.mark.parametrize("body", [
    _job({"kind": "prefix"}, extraction_mode="general", mixed_invoice=True),
    _job({"kind": "prefix"}, extraction_mode="native_only", refresh_ocr=True),
    _job({"kind": "prefix"}, extraction_mode="auto"),
    _job({"kind": "keys", "keys": []}),
    _job({"kind": "keys", "keys": ["a.pdf"], "prefix": "x/"}),
    _job({"kind": "prefix", "prefix": "x/", "keys": ["a.pdf"]}),
    {"scope": {"kind": "prefix"}},                    # 缺 options:新任务必须显式传提取方式
    {"scope": {"kind": "prefix"}, "options": {}},     # 缺 extraction_mode
])
def test_illegal_options_rejected_with_422(client, kb_env, body):
    assert client.post(f"{ADMIN}/index-jobs", json=body).status_code == 422


def test_unknown_job_404(client, kb_env):
    assert client.get(f"{ADMIN}/index-jobs/nope").status_code == 404
    assert client.get(f"{ADMIN}/index-jobs/nope/files").status_code == 404


def test_busy_job_conflicts_with_409(client, kb_env, monkeypatch):
    other = {"job_id": "other-job", "pid": 1, "started_at": "2026-10-03T00:00:00Z",
             "heartbeat": time.time()}          # 新鲜心跳 = 真的有任务在跑
    ocr_jobs.jobs_dir().mkdir(parents=True, exist_ok=True)
    ocr_jobs._write({"job_id": "other-job"})    # 占位,避免 files/ 不存在
    from app.rag.localfs import atomic_write_json
    atomic_write_json(ocr_jobs._lock_path(), other)

    r = client.post(f"{ADMIN}/index-jobs", json=_job({"kind": "prefix"}))
    assert r.status_code == 409
    assert "other-job" in r.json()["detail"]


def test_stale_lock_is_taken_over(client, kb_env):
    from app.rag.localfs import atomic_write_json
    atomic_write_json(ocr_jobs._lock_path(), {
        "job_id": "dead-job", "pid": 999999, "started_at": "2020-01-01T00:00:00Z",
        "heartbeat": time.time() - ocr_jobs.LOCK_STALE_SECONDS - 60,
    })
    kb_env.kb.files["a.pdf"] = make_pdf(pages=[{"text": "x"}])
    r = client.post(f"{ADMIN}/index-jobs", json=_job({"kind": "prefix"}))
    assert r.status_code == 202
    assert _wait(client, r.json()["job_id"])["status"] == "published"


# ---- 版本指针 / 回退接口 ----

def test_manifest_and_rollback_endpoint(client, kb_env):
    kb_env.kb.files["a.pdf"] = make_pdf(pages=[{"text": "first version"}])
    job_id = client.post(f"{ADMIN}/index-jobs", json=_job({"kind": "prefix"})).json()["job_id"]
    v1 = _wait(client, job_id)["summary"]["index_version"]

    kb_env.kb.files["a.pdf"] = make_pdf(pages=[{"text": "second version"}])
    job_id = client.post(f"{ADMIN}/index-jobs", json=_job({"kind": "prefix"})).json()["job_id"]
    v2 = _wait(client, job_id)["summary"]["index_version"]
    assert v2 != v1

    info = client.get(f"{ADMIN}/index-manifest").json()
    assert info["active"] == v2 and info["previous"] == v1
    roles = {v["name"]: v["role"] for v in info["versions"]}
    assert roles[v2] == "active" and roles[v1] == "previous"

    # 发布 / 回退记录按时间序(旧→新)给出,前端反转展示成「最近几次」
    his = info["history"]
    assert [h["action"] for h in his] == ["publish", "publish"]
    assert his[-1]["name"] == v2 and his[-1]["replaced"] == v1
    assert isinstance(his[-1]["points"], int) and his[-1]["points"] > 0

    back = client.post(f"{ADMIN}/index-manifest/rollback").json()
    assert back["active"] == v1 and back["previous"] == v2
    tail = back["history"][-1]
    assert tail["action"] == "rollback" and tail["name"] == v1 and tail["replaced"] == v2

    r = client.post(f"{ADMIN}/index-manifest/rollback")
    assert r.status_code == 200


def test_manifest_history_returns_recent_tail(client, kb_env):
    """history 只回传最近若干条(旧→新),不把 manifest 里最多的 50 条全丢给前端。"""
    from app.api.routes.knowledge import _HISTORY_TAIL
    from app.rag.localfs import atomic_write_json

    entries = [
        {"at": f"2026-10-0{i}T00:00:00+00:00", "action": "publish", "name": f"v{i}",
         "replaced": None, "points": i, "note": ""}
        for i in range(1, _HISTORY_TAIL + 4)
    ]
    atomic_write_json(store.manifest_path(), {"collection": "lab", "active": f"v{_HISTORY_TAIL + 3}",
                                              "history": entries})

    info = client.get(f"{ADMIN}/index-manifest").json()
    assert [h["name"] for h in info["history"]] == [f"v{i}" for i in range(4, _HISTORY_TAIL + 4)]


def test_rollback_refused_while_job_running(client, kb_env):
    from app.rag.localfs import atomic_write_json
    atomic_write_json(ocr_jobs._lock_path(), {"job_id": "run", "heartbeat": time.time()})
    assert client.post(f"{ADMIN}/index-manifest/rollback").status_code == 409


def test_reconcile_marks_running_job_failed(kb_env):
    job = ocr_jobs.new_job(ingest.Scope(kind="prefix"), ingest.Options())
    ocr_jobs._write({**job, "status": "running"})
    assert ocr_jobs.reconcile() == 1
    after = ocr_jobs.get_job(job["job_id"])
    assert after["status"] == "failed" and "重启" in after["error"]
    assert ocr_jobs.lock_owner() is None


def test_reconcile_retries_pending_cache_cleanup(kb_env):
    """重启对账补完没清完的文件缓存:只删缓存与登记,不重跑 OCR(§8)。"""
    from app.rag import documents

    doc_id = documents.register("a.pdf", content_sha256="sha")
    documents.write_manifest(documents.build_manifest(
        document_id=doc_id, oss_key="a.pdf", content_sha256="sha", extraction_mode="general",
        mixed_invoice=False, index_version="v1", pages=[(1, "ocr-k", "text-k")],
        embedding_keys=[]))
    kb_env.cache.set("ocr-k", "x")
    pending = documents.prepare_deletion("a.pdf")           # 上次删到一半中断,恢复记录还在
    documents.mark_deletion(pending, object_deleted=True, vectors_deleted=True)

    ocr_jobs.reconcile()

    assert kb_env.cache.get("ocr-k") is None
    assert documents.manifest_of(doc_id) is None
    assert not documents.deletion_record_path(doc_id).exists()


# ---- 同步旧入口已删除 ----

@pytest.mark.parametrize("path,body", [
    ("/index", {"key": "a.pdf"}),           # 旧:同步单文件索引
    ("/reindex", {"prefix": ""}),           # 旧:同步全量 / 子树重建
])
def test_legacy_sync_endpoints_are_gone(client, kb_env, path, body):
    """生产未上线,不留兼容层:索引一律走 /index-jobs(202 + 轮询)。"""
    assert client.post(f"{ADMIN}{path}", json=body).status_code == 404


def test_indexed_badges_read_active_version(client, kb_env):
    kb_env.kb.files["d/a.pdf"] = make_pdf(pages=[{"text": "body"}])
    job_id = client.post(f"{ADMIN}/index-jobs",
                         json=_job({"kind": "prefix", "prefix": "d/"})).json()["job_id"]
    _wait(client, job_id)
    keys = client.get(f"{ADMIN}/indexed", params={"prefix": "d/"}).json()["keys"]
    assert keys == ["d/a.pdf"]
