"""SOP 增删改查路由的离线单测:走内存假仓库(见 conftest.install_sops),不触真实 OSS。

覆盖:列表 / 详情 / 新建(含 409 重名、400 非法 id)/ 更新(含 404、400 id 不一致)/
删除(含 404);并验证每次写后 loader.reload() 让 LLM 侧目录即时同步。
"""

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.skills import loader

_PREFIX = get_settings().api_prefix
_URL = f"{_PREFIX}/sops"

_TRAVEL = """---
id: travel
name: 差旅报销
description: 出差交通住宿费用报销
triggers:
  - 差旅
  - 高铁
---
# 差旅报销
1. 填报销单
"""


def _client() -> TestClient:
    return TestClient(app)


def test_list_empty(install_sops):
    install_sops({})
    r = _client().get(_URL)
    assert r.status_code == 200
    assert r.json() == []


def test_list_returns_summaries(install_sops):
    install_sops({"travel.md": _TRAVEL})
    r = _client().get(_URL)
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    item = items[0]
    assert item["id"] == "travel"
    assert item["name"] == "差旅报销"
    assert item["triggers"] == ["差旅", "高铁"]
    assert item["key"] == "sops/travel.md"
    assert item["updated_at"] == 1_700_000_000
    assert "body" not in item  # 列表不含正文


def test_get_detail(install_sops):
    install_sops({"travel.md": _TRAVEL})
    r = _client().get(f"{_URL}/travel")
    assert r.status_code == 200
    d = r.json()
    assert d["id"] == "travel"
    assert d["key"] == "sops/travel.md"
    assert "填报销单" in d["body"]


def test_get_missing_404(install_sops):
    install_sops({})
    assert _client().get(f"{_URL}/nope").status_code == 404


def test_get_invalid_id_400(install_sops):
    install_sops({})
    # FastAPI 不会把 %2F 之类拆进 path 参数;这里测显式非法字符
    assert _client().get(f"{_URL}/bad.id").status_code == 400


def test_create_then_readable_and_reloaded(install_sops):
    install_sops({})
    client = _client()
    payload = {
        "id": "supplies",
        "name": "办公用品报销",
        "description": "耗材采购报销",
        "triggers": ["办公用品", "耗材"],
        "body": "# 办公用品报销\n1. 提交采购申请\n",
    }
    r = client.post(_URL, json=payload)
    assert r.status_code == 201
    d = r.json()
    assert d["id"] == "supplies"
    assert d["key"] == "sops/supplies.md"
    assert d["triggers"] == ["办公用品", "耗材"]
    assert "提交采购申请" in d["body"]
    # 列表能看到
    assert {i["id"] for i in client.get(_URL).json()} == {"supplies"}
    # loader 已 reload,LLM 侧目录即时可见
    assert loader.get_skill("supplies") is not None


def test_create_duplicate_409(install_sops):
    install_sops({"travel.md": _TRAVEL})
    r = _client().post(_URL, json={"id": "travel", "name": "重复", "body": "x"})
    assert r.status_code == 409


def test_create_invalid_id_400(install_sops):
    install_sops({})
    r = _client().post(_URL, json={"id": "bad/id", "name": "x", "body": "y"})
    assert r.status_code == 400


def test_update_changes_content(install_sops):
    install_sops({"travel.md": _TRAVEL})
    client = _client()
    r = client.put(
        f"{_URL}/travel",
        json={"id": "travel", "name": "差旅报销 v2", "description": "", "triggers": [], "body": "# v2\n"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "差旅报销 v2"
    # 回读确认落盘
    assert "v2" in client.get(f"{_URL}/travel").json()["body"]


def test_update_missing_404(install_sops):
    install_sops({})
    r = _client().put(f"{_URL}/nope", json={"id": "nope", "name": "x", "body": "y"})
    assert r.status_code == 404


def test_update_id_mismatch_400(install_sops):
    install_sops({"travel.md": _TRAVEL})
    r = _client().put(f"{_URL}/travel", json={"id": "other", "name": "x", "body": "y"})
    assert r.status_code == 400


def test_delete_then_gone_and_reloaded(install_sops):
    install_sops({"travel.md": _TRAVEL})
    client = _client()
    assert loader.get_skill("travel") is not None
    r = client.delete(f"{_URL}/travel")
    assert r.status_code == 204
    assert client.get(f"{_URL}/travel").status_code == 404
    assert loader.get_skill("travel") is None  # reload 后 LLM 侧目录同步清掉


def test_delete_missing_404(install_sops):
    install_sops({})
    assert _client().delete(f"{_URL}/nope").status_code == 404
