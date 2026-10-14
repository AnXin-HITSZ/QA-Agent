"""Opt-in real dev MySQL/API check; isolated scope, no paid model calls."""
import os
import uuid
import json
from pathlib import Path
import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient
from app.config import get_settings
from app.memory import db, repo, service
from app.auth.deps import require_user
from app.auth.service import Principal
from app.main import app


@pytest.mark.skipif(os.getenv("MEMORY_TEST_EXPERIENCE") != "1", reason="real dev experience check not enabled")
def test_real_content_search_utc_and_cleanup(monkeypatch):
    settings = get_settings()
    assert settings.app_env == "dev" and settings.memory_collection == "user_memory_eval_dev"
    uid = str(uuid.uuid4())
    scope = "eval:memory-management-" + uuid.uuid4().hex
    with db.session_scope() as session:
        assert session.execute(text("SELECT DATABASE()")).scalar() == "qa_agent_dev"
    principal = Principal(uid, "test@example.invalid", "isolated-test", "user", "active", 1, "test")
    real_list = service.list_items
    monkeypatch.setattr(service, "list_items", lambda **kw: real_list(**kw, scope=scope))
    previous = app.dependency_overrides.get(require_user)
    app.dependency_overrides[require_user] = lambda: principal
    root = Path("data/eval/memory-ui-20261014")
    root.mkdir(parents=True, exist_ok=True)
    report = {"scope": scope, "user_id": uid}
    try:
        with db.session_scope() as session:
            for content in ("测试聚餐安排", "测试编程课程", "测试费用100%"):
                repo.create_item(session, user_id=uid, scope=scope, text=content, now=db.utc_naive())
        client = TestClient(app)
        matched = client.get("/api/v1/memory", params={"q": "聚餐"})
        assert matched.status_code == 200, matched.text
        body = matched.json()
        assert body["total"] == 1 and body["items"][0]["text"] == "测试聚餐安排"
        assert body["items"][0]["updated_at"].endswith(("Z", "+00:00"))
        assert body["items"][0]["index_state"] == "pending"
        assert client.get("/api/v1/memory", params={"q": "unrelatedzzzz"}).json()["total"] == 0
        assert client.get("/api/v1/memory", params={"q": "%"}).json()["total"] == 1
        assert client.get("/api/v1/memory", params={"limit": 1, "offset": 1}).json()["total"] == 3
        report["api_checks_passed"] = True
    finally:
        if previous is None:
            app.dependency_overrides.pop(require_user, None)
        else:
            app.dependency_overrides[require_user] = previous
        report["purge"] = service.clear_user(user_id=uid, scope=scope)
        with db.session_scope() as session:
            report["items_remaining"] = repo.count_items_in_scope(session, user_id=uid, scope=scope)
        report["clean"] = report["items_remaining"] == 0 and report["purge"]["cleanup"] == "done"
        (root/"real-management-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        assert report["clean"]
