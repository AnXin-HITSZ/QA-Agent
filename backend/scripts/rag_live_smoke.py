"""Opt-in paid RAG smoke against development services, with owned-resource cleanup.

Uses actual HTTP routes/auth, MySQL, OSS, Redis, Qdrant, embeddings and chat LLM.
Application-wide startup reconciliation/workers are intentionally not started: the
probe must not consume other users' pending jobs or send their todos to a model.
No tokens, passwords, signed URLs or service credentials are written to reports.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--node", type=Path)
    parser.add_argument("--skip-ocr", action="store_true")
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error("Real paid calls require --allow-paid")
    os.chdir(Path(__file__).resolve().parent.parent)
    logging.disable(logging.CRITICAL)
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    from app.config import get_settings
    s = get_settings()
    db_name = urlparse(s.mysql_url).path.strip("/")
    if not re.search(r"(^|_)(dev|test)($|_)", db_name):
        print(json.dumps({"ok": False, "stage": "configuration", "reason": "A development/test MySQL database is required"}))
        return 2
    if not all((s.embeddings_api_key, s.llm_api_key, s.qdrant_url, s.redis_url, s.oss_bucket)):
        print(json.dumps({"ok": False, "stage": "configuration", "reason": "Missing live service configuration"}))
        return 2
    run = uuid.uuid4().hex[:12]
    base = s.qdrant_collection + "_rag_smoke_" + run
    root = Path("data/rag-live-smoke") / run
    root.mkdir(parents=True, exist_ok=False)
    report = {"run_id": run, "ok": False, "checks": {}, "limitations": [
        "Application-wide workers/reconciliation are not part of this isolated HTTP probe",
        "Synthetic documents verify functionality, not real-corpus retrieval quality"]}
    s.qdrant_collection = base
    s.oss_prefix = s.oss_prefix.rstrip("/") + "/__rag_smoke/" + run + "/"
    s.oss_sops_prefix = s.oss_prefix + "sops/"
    s.app_env = "rag-smoke-" + run
    s.index_state_dir = str(root / "index")
    s.metering_pending_dir = str(root / "metering")
    s.llm_request_timeout = min(s.llm_request_timeout, 60)
    host = urlparse(s.qdrant_url).hostname or ""
    os.environ["NO_PROXY"] = ",".join(filter(None, [host, "127.0.0.1", "localhost", os.environ.get("NO_PROXY", "")]))
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    from app.rag import cache_store, documents, oss, store
    from app.auth import db as auth_db
    from app.auth.service import AuthService
    from app.graph import build_graph
    from app.main import app
    from app import metering
    from scripts.create_admin import create_admin
    import httpx
    import uvicorn
    from tests.pdfkit import make_pdf
    keys = [run + "/manual.pdf", run + "/rules.md"]
    created_keys: list[str] = []
    user_id = None
    server = None
    server_thread = None
    sock = None
    front = None
    front_log = None
    stage = "preflight"
    cleanup_errors = []
    try:
        cache_store.ensure_available()
        store.get_client().get_collections()
        ok, _ = auth_db.ping()
        if not ok:
            raise RuntimeError("Development database connectivity failed")
        report["checks"]["service_preflight"] = True
        password = "RagSmoke!" + secrets.token_urlsafe(20) + "9"
        email = f"rag-smoke-{run}@example.com"
        user_id = create_admin(email=email, password=password, display_name="RAG isolated smoke")
        kb = oss.knowledge_store()
        stage = "synthetic_upload"
        files = {
            keys[0]: make_pdf(pages=[
                {"text": f"RAG-SMOKE-{run}: Equipment A borrowing requires approval by Manager Lin."},
                {"text": f"RAG-SMOKE-{run}: Equipment A return inspection is performed by Engineer Chen."}]),
            keys[1]: (f"# RAG-SMOKE-{run} 设备规则\n\n## 设备 B\n\n设备 B 外借由管理员周老师审批。\n\n"
                      "| 步骤 | 操作 |\n| --- | --- |\n| 1 | 先登记设备 |\n| 2 | 再提交审批 |\n").encode(),
        }
        for key, data in files.items():
            kb.put_object(key, data)
            created_keys.append(key)
        metering.start()
        # Real app routes and graph, but no global background work or shared todos.
        app.state.auth = AuthService()
        app.state.graph = build_graph().with_config(recursion_limit=8)
        app.state.checkpointer = None
        app.state.todos = None
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        config = uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", access_log=False, log_level="critical")
        server = uvicorn.Server(config)
        server_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        server_thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.1)
        if not server.started:
            raise RuntimeError("HTTP server did not start")
        report["http_port"] = port
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=180, trust_env=False) as client:
            stage = "http_auth"
            assert client.get("/health").json() == {"status": "ok"}
            assert client.get("/api/v1/knowledge/tree").status_code == 401
            login = client.post("/api/v1/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, "Real login failed"
            access = login.json()["access_token"]
            client.headers["Authorization"] = "Bearer " + access
            report["checks"]["http_auth"] = True

            def job(selected, mode):
                response = client.post("/api/v1/admin/knowledge/index-jobs", json={
                    "scope": {"kind": "keys", "keys": selected},
                    "options": {"extraction_mode": mode}})
                assert response.status_code == 202, "Index job creation failed"
                job_id = response.json()["job_id"]
                until = time.monotonic() + 180
                while time.monotonic() < until:
                    status = client.get("/api/v1/admin/knowledge/index-jobs/" + job_id)
                    status.raise_for_status()
                    body = status.json()
                    if body["status"] in ("published", "failed"):
                        assert body.get("published"), "Index job did not publish"
                        return body
                    time.sleep(.3)
                raise RuntimeError("Index job timeout")

            stage = "native_index"
            first = job(keys, "native_only")
            report["checks"]["native_index"] = {"published": True, "files": first["summary"]["indexed_files"],
                                                  "vectors": first["summary"]["vectors"]}
            current = store.active_collection()
            count = store.collection_count(current)
            second = job(keys, "native_only")
            assert store.collection_count(store.active_collection()) == count
            report["checks"]["reindex"] = {"published": True, "stable_vector_count": True}
            if not args.skip_ocr:
                stage = "ocr_index"
                from PIL import Image, ImageDraw, ImageFont
                image = Image.new("RGB", (1600, 350), "white")
                draw = ImageDraw.Draw(image)
                font_path = Path("C:/Windows/Fonts/arial.ttf")
                font = ImageFont.truetype(str(font_path), 38) if font_path.exists() else ImageFont.load_default()
                draw.text((30, 40), f"RAG SMOKE {run}", fill="black", font=font)
                draw.text((30, 130), "Equipment C approval: Manager Wang", fill="black", font=font)
                image_bytes = io.BytesIO()
                image.save(image_bytes, format="PNG")
                image_key = run + "/ocr.png"
                kb.put_object(image_key, image_bytes.getvalue())
                created_keys.append(image_key)
                recognized = job([image_key], "general")
                assert recognized["summary"]["pages"]["ok"] == 1
                reused = job([image_key], "general")
                assert reused["summary"]["pages"]["cached"] == 1
                report["checks"]["ocr"] = {"recognized": True, "second_run_cache_hit": True}
            stage = "retrieval"
            from app.rag.retrieve import search_knowledge
            hits = search_knowledge(f"RAG-SMOKE-{run} Equipment A approval Manager Lin", top_k=5)
            pdf_hits = [h for h in hits if h["oss_key"] == keys[0]]
            assert pdf_hits and "Manager Lin" in pdf_hits[0]["text"]
            assert 1 in pdf_hits[0]["page_numbers"]
            report["checks"]["retrieval"] = {"pdf_found": True, "pages": pdf_hits[0]["page_numbers"], "hit_count": len(hits)}
            stage = "chat"
            question = (f"Please search the knowledge base for RAG-SMOKE-{run}. According to the PDF manual, "
                        "who approves borrowing Equipment A? Answer in English using the retrieved evidence.")
            answer = client.post("/api/v1/chat", json={"message": question})
            assert answer.status_code == 200, "Real chat request failed"
            payload = answer.json()
            assert "Lin" in payload["content"] and payload["sources"], "Chat evidence or answer missing"
            assert any(src["oss_key"] == keys[0] and 1 in src.get("page_numbers", []) for src in payload["sources"])
            report["checks"]["chat"] = {"answer": payload["content"], "source_count": len(payload["sources"]), "pdf_page_cited": True}
            stage = "stream"
            streamed = client.post("/api/v1/chat/stream", json={"message": question})
            assert streamed.status_code == 200 and "event: done" in streamed.text and "event: error" not in streamed.text
            done = None
            event = ""
            for line in streamed.text.splitlines():
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event == "done":
                    done = json.loads(line.split(":", 1)[1])
            assert done and any(src["oss_key"] == keys[0] and 1 in src.get("page_numbers", []) for src in done["sources"])
            report["checks"]["stream"] = {"done_received": True, "pdf_page_cited": True}
            if args.node:
                stage = "frontend_proxy"
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    front_port = probe.getsockname()[1]
                frontend = Path(__file__).resolve().parents[2] / "frontend"
                front_log = (root / "vite.log").open("w", encoding="utf-8")
                env = dict(os.environ, QA_AGENT_API_TARGET=f"http://127.0.0.1:{port}")
                front = subprocess.Popen([str(args.node), "node_modules/vite/bin/vite.js", "--host", "127.0.0.1",
                                          "--port", str(front_port), "--strictPort"], cwd=frontend, env=env,
                                         stdout=front_log, stderr=front_log,
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                with httpx.Client(base_url=f"http://127.0.0.1:{front_port}", timeout=15, trust_env=False) as browser:
                    for _ in range(100):
                        try:
                            home = browser.get("/")
                            if home.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if front.poll() is not None:
                            raise RuntimeError("Vite startup failed")
                        time.sleep(.1)
                    else:
                        raise RuntimeError("Vite startup timeout")
                    assert "<html" in home.text.lower()
                    proxied = browser.post("/api/v1/auth/login", json={"email": email, "password": password})
                    assert proxied.status_code == 200
                    report["checks"]["frontend_proxy"] = {"page_served": True, "real_login_proxied": True}
            report["ok"] = True
    except Exception as exc:
        report["failure"] = {"stage": stage, "error_type": type(exc).__name__}
    finally:
        if front is not None:
            front.terminate()
            try:
                front.wait(timeout=10)
            except subprocess.TimeoutExpired:
                front.kill()
                front.wait(timeout=5)
        if front_log:
            front_log.close()
        if server:
            server.should_exit = True
        if server_thread:
            server_thread.join(timeout=15)
        if sock:
            sock.close()
        try:
            for name in [c.name for c in store.get_client().get_collections().collections]:
                if name == base or name.startswith(base + "__"):
                    store.get_client().delete_collection(name)
        except Exception as exc:
            cleanup_errors.append("qdrant:" + type(exc).__name__)
        for key in created_keys:
            try:
                oss.knowledge_store().delete_object(key)
                documents.forget_file(key)
            except Exception as exc:
                cleanup_errors.append("file:" + type(exc).__name__)
        if user_id:
            try:
                from sqlalchemy import delete, inspect, or_
                from app.auth import tables as auth_tables
                from app.memory import tables as memory_tables
                existing = set(inspect(auth_db.get_engine()).get_table_names())
                with auth_db.session_scope() as session:
                    for table in reversed(list(memory_tables.Base.metadata.tables.values())):
                        if table.name in existing and "user_id" in table.c:
                            session.execute(delete(table).where(table.c.user_id == user_id))
                    for table in reversed(list(auth_tables.Base.metadata.tables.values())):
                        if "user_id" in table.c:
                            session.execute(delete(table).where(table.c.user_id == user_id))
                    session.execute(delete(auth_tables.AuthAuditRow).where(or_(auth_tables.AuthAuditRow.actor_user_id == user_id,
                        auth_tables.AuthAuditRow.target_user_id == user_id)))
                    session.execute(delete(auth_tables.UserRow).where(auth_tables.UserRow.id == user_id))
            except Exception as exc:
                cleanup_errors.append("test_user:" + type(exc).__name__)
        try:
            from sqlalchemy import select
            from app.metering import db as metering_db
            from app.metering.tables import CallEventRow
            job_ids = [json.loads(path.read_text(encoding="utf-8"))["job_id"]
                       for path in (root / "index/jobs").glob("*.json")]
            if job_ids and s.metering_enabled:
                with metering_db.session_scope() as session:
                    rows = session.execute(select(CallEventRow.job_id, CallEventRow.service,
                        CallEventRow.status, CallEventRow.usage_quantity, CallEventRow.usage_unit,
                        CallEventRow.cost_amount, CallEventRow.currency).where(CallEventRow.job_id.in_(job_ids))).all()
                report["index_call_events"] = [dict(job_id=row[0], service=row[1], status=row[2],
                    usage=str(row[3]) if row[3] is not None else None, unit=row[4],
                    estimated_cost=str(row[5]) if row[5] is not None else None, currency=row[6]) for row in rows]
                report["billing_note"] = "Index-job events only; excludes query embeddings and chat LLM, not an official bill"
        except Exception as exc:
            report["metering_report_error"] = type(exc).__name__
        try:
            metering.stop()
        except Exception as exc:
            cleanup_errors.append("metering:" + type(exc).__name__)
        report["cleanup"] = {"ok": not cleanup_errors, "errors": cleanup_errors}
        if cleanup_errors:
            report["ok"] = False
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": report["ok"], "checks": list(report["checks"]), "failure": report.get("failure"),
                          "cleanup": report["cleanup"], "report": str((root / "report.json").resolve())}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
