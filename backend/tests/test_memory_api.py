"""「我的记忆」接口(§12):归属、权限、开关语义、删除的确认。

身份用 autouse 的「已登录」桩(tests/conftest.py::_stub_auth):**这正是要测的姿态** ——
user_id 只可能来自 Principal。用例里有两条专门钉这件事:
- `test_no_interface_accepts_a_user_id`:结构用例,接口的参数里根本没有 user_id 这种东西;
- `test_foreign_id_looks_like_it_does_not_exist`:拿别人的 id 来打,一律 404 且不动数据。

外部依赖全部换成假件(内存 Qdrant / 假 Embeddings / 脚本化召回),不碰真实服务。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.memory import repo, service, vector
from tests import memorykit
from tests.conftest import STUB_ADMIN_ID

API = "/api/v1/memory"
OTHER = "00000000-0000-4000-8000-0000000000ff"

TEXT_A = "用户偏好用中文写实验记录"
TEXT_B = "用户喜欢喝咖啡"

API_PARAMS = {
    "memory_vector_k": 3, "memory_bm25_k": 3, "memory_rerank_k": 3, "memory_top_k": 3,
    "memory_rrf_k": 60, "memory_context_chars": 2000, "memory_bm25_corpus_limit": 1000,
}


@pytest.fixture
def env(memory_db, cache_env, monkeypatch):
    """离线记忆环境 + 测试客户端(身份是 conftest 里的已登录桩)。"""
    s = memory_db.settings
    kit = memorykit.install(monkeypatch, settings=s, dim=s.embeddings_dim, params=API_PARAMS)
    kit.db = memory_db.db
    kit.cache = cache_env
    kit.client = TestClient(app, raise_server_exceptions=False)
    kit.other = OTHER
    return kit


def _seed(kit, *texts: str, user_id: str = STUB_ADMIN_ID) -> list[str]:
    return memorykit.seed(kit, kit.db, *texts, user_id=user_id)


def _items(response) -> list[dict]:
    assert response.status_code == 200, response.text
    return response.json()["items"]


# ---- 列表与检索 ----


def test_list_returns_my_memories_with_source_and_index_state(env):
    a, b = _seed(env, TEXT_A, TEXT_B)
    _seed(env, "别人的事实", user_id=OTHER)

    rows = _items(env.client.get(API))

    assert {r["id"] for r in rows} == {a, b}
    assert all(r["status"] == "active" and r["origin"] == "llm" for r in rows)
    assert all(r["index_state"] == "synced" and r["indexed_at"] for r in rows)
    assert all(r["created_at"] and r["updated_at"] for r in rows)
    body = env.client.get(API).json()
    assert body["enabled"] is True and body["degraded"] == [] and body["error"] == ""
    assert body["index_pending"] == 0 and body["total"] == 2


def test_list_pages(env):
    _seed(env, TEXT_A, TEXT_B, "用户每周五整理实验数据")

    page1 = env.client.get(API, params={"limit": 2, "offset": 0}).json()
    page2 = env.client.get(API, params={"limit": 2, "offset": 2}).json()

    assert len(page1["items"]) == 2 and page1["total"] == 3
    assert len(page2["items"]) == 1
    assert not ({i["id"] for i in page1["items"]} & {i["id"] for i in page2["items"]})
    assert env.client.get(API, params={"limit": 0}).status_code == 422      # 越界参数明确拒绝


def test_search_uses_retrieval_and_echoes_the_query(env):
    a, b = _seed(env, TEXT_A, TEXT_B)
    env.dense.ids = [b]

    body = env.client.get(API, params={"q": "zzz"}).json()      # 与正文无共同词项 → 只走向量那一路

    assert [i["id"] for i in body["items"]] == [b]
    assert body["query"] == "zzz"


def test_a_pending_index_is_visible_but_the_memory_is_still_listed(env):
    """索引是派生数据:pending 也要照常显示(不然用户以为没保存),并如实标出同步状态。"""
    with env.db.session_scope() as session:
        item = repo.create_item(session, user_id=STUB_ADMIN_ID, text=TEXT_A,
                                now=datetime(2026, 10, 8, 12, 0, 0))   # 不走索引=待索引

    body = env.client.get(API).json()

    assert [i["id"] for i in body["items"]] == [item.id]
    assert body["items"][0]["index_state"] == "pending"
    assert body["items"][0]["indexed_at"] is None
    assert body["index_pending"] == 1

    reindexed = env.client.post(f"{API}/reindex").json()

    assert reindexed == {"requested": 1, "indexed": 1, "payload_only": 0, "deferred": 0,
                         "error": ""}
    assert env.client.get(API).json()["items"][0]["index_state"] == "synced"
    assert vector.count(STUB_ADMIN_ID) == 1


# ---- 增 / 改 / 删 ----


def test_add_edit_delete_round_trip(env):
    created = env.client.post(API, json={"text": "  我的电话是10086  "})
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["text"] == "我的电话是10086" and item["origin"] == "user"
    assert item["revision"] == 1 and item["index_state"] == "synced"

    edited = env.client.patch(f"{API}/{item['id']}", json={"text": "我的电话是10010"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] == 2 and edited.json()["text"] == "我的电话是10010"

    gone = env.client.delete(f"{API}/{item['id']}")
    assert gone.status_code == 200, gone.text
    # 事实删掉之外,索引侧的清理结果也如实回给界面(清干净了才报 done)
    assert gone.json() == {"id": item["id"], "cleanup": "done", "degraded": []}
    assert env.client.get(API).json()["items"] == []
    assert env.client.delete(f"{API}/{item['id']}").status_code == 404      # 已删的不再存在
    assert vector.count(STUB_ADMIN_ID) == 0

    events = env.client.get(f"{API}/history").json()["items"]
    assert [e["event"] for e in events] == ["DELETE", "UPDATE", "ADD"]      # 新的在前
    assert events[0]["actor"] == "user" and events[0]["new_text"] is None


def test_add_rejects_blank_and_duplicate(env):
    assert env.client.post(API, json={"text": "爱吃辣"}).status_code == 201
    assert env.client.post(API, json={"text": " 爱吃辣 "}).status_code == 409
    assert env.client.post(API, json={"text": "   "}).status_code == 409
    assert env.client.post(API, json={}).status_code == 422                 # 缺字段
    assert len(env.client.get(API).json()["items"]) == 1


def test_foreign_id_looks_like_it_does_not_exist(env):
    """别人的记忆:改 / 删一律 404,而且一个字段都没动(不泄露「这条存在」)。"""
    (foreign,) = _seed(env, "别人的事实", user_id=OTHER)

    assert env.client.patch(f"{API}/{foreign}", json={"text": "改成我的"}).status_code == 404
    assert env.client.delete(f"{API}/{foreign}").status_code == 404
    assert env.client.get(f"{API}/{foreign}").status_code == 405            # 没有「按 id 读单条」这种接口

    with env.db.session_scope() as session:
        assert repo.get_item(session, OTHER, foreign).text == "别人的事实"


def test_history_only_returns_mine(env):
    a, _ = _seed(env, TEXT_A, TEXT_B)
    _seed(env, "别人的事实", user_id=OTHER)

    body = env.client.get(f"{API}/history").json()

    assert body["enabled"] is True
    assert {h["new_text"] for h in body["items"]} == {TEXT_A, TEXT_B}
    mine = env.client.get(f"{API}/history", params={"memory_id": a}).json()["items"]
    assert [h["new_text"] for h in mine] == [TEXT_A]


# ---- 彻底清除 ----


def test_clear_needs_an_explicit_confirmation(env):
    _seed(env, TEXT_A)

    refused = env.client.delete(API)

    assert refused.status_code == 400
    assert "confirm" in refused.json()["detail"]
    assert len(env.client.get(API).json()["items"]) == 1       # 没确认就什么都没删


def test_clear_reports_what_it_did(env):
    _seed(env, TEXT_A, TEXT_B)
    service.enqueue_extraction(user_id=STUB_ADMIN_ID, dedupe_key="k1",
                               messages=[{"role": "user", "content": "我住在深圳"}])

    body = env.client.delete(API, params={"confirm": True}).json()

    assert body["items"] == 2 and body["jobs"] == 1 and body["history"] >= 2
    assert body["generation"] == 1 and body["vectors"] is True and body["degraded"] == []
    assert env.client.get(API).json()["items"] == []
    assert vector.count(STUB_ADMIN_ID) == 0
    audit = env.client.get(f"{API}/history").json()["items"]      # 审计保留,正文脱敏
    assert audit and all(h["old_text"] is None and h["new_text"] is None for h in audit)


def test_a_delete_whose_vector_cleanup_failed_is_reported_as_still_cleaning(env, monkeypatch):
    """删除是两步:事实立刻删掉,向量清理可能要后台重试 —— 接口照实报 pending,状态页也看得见。

    如果这里报成 done,界面就会说「已全部清干净」:那是把没做完的事说成做完了。
    反过来也不能因为向量没清干净就报错 —— 用户要的「这条不再被使用」已经生效了。
    """
    _seed(env, TEXT_A)
    item_id = _items(env.client.get(API))[0]["id"]
    monkeypatch.setattr(vector, "delete_ids",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("qdrant 不可达")))

    gone = env.client.delete(f"{API}/{item_id}")

    assert gone.status_code == 200                              # 不是错误:删除本身已经生效
    body = gone.json()
    assert body["id"] == item_id and body["cleanup"] == "pending"
    assert len(body["degraded"]) == 1 and "重试" in body["degraded"][0]   # 如实说明没做到最好的一步
    assert _items(env.client.get(API)) == []                    # 事实层立刻不再被使用
    assert vector.count(STUB_ADMIN_ID) == 1                     # 向量还有残留(后台接着清)
    status = env.client.get(f"{API}/status").json()
    assert status["cleanup_pending"] == 1 and status["cleanup_failed"] == 0

    # 重试用尽 → 状态页如实说「清理失败」(这时已经没有任何地方会再重试它了)
    with env.db.session_scope() as session:
        op = repo.claim_op(session, owner="test-worker", now=env.db.utc_naive(),
                           lease_seconds=60)
        assert repo.fail_op(session, op_id=op.id, owner="test-worker",
                            claim_token=op.claim_token, error="RuntimeError",
                            now=env.db.utc_naive(), permanent=True)
    status = env.client.get(f"{API}/status").json()
    assert status["cleanup_pending"] == 0 and status["cleanup_failed"] == 1
    assert "sparse_search" in status                            # 降级通道口径也如实暴露


# ---- 开关(§12:分开说清,关闭不删数据) ----


def test_status_reports_every_switch(env):
    _seed(env, TEXT_A)

    body = env.client.get(f"{API}/status").json()

    assert body["enabled"] is True and body["configured"] is True
    assert body["write_enabled"] is True and body["search_enabled"] is True
    assert body["maintenance_enabled"] is True
    assert body["items"] == 1 and body["deleted_items"] == 0 and body["index_pending"] == 0
    assert body["jobs"]["succeeded"] == 0 and body["jobs"]["failed"] == 0
    assert body["last_run_at"] == "" and body["last_error"] == ""


def test_pausing_automatic_writes_keeps_every_memory_and_still_allows_manual_edits(env, monkeypatch):
    """暂停自动写入 = 不再登记新提取;**不是**删数据,也不拦用户自己动手。"""
    (a,) = _seed(env, TEXT_A)
    monkeypatch.setattr(env.settings, "memory_write_enabled", False)

    assert env.client.get(f"{API}/status").json()["write_enabled"] is False
    assert len(env.client.get(API).json()["items"]) == 1                    # 记忆还在
    assert service.enqueue_extraction(user_id=STUB_ADMIN_ID, dedupe_key="k1",
                                      messages=[{"role": "user",
                                                 "content": "我住在深圳"}]) is None
    assert env.client.post(API, json={"text": "手动加一条"}).status_code == 201
    assert env.client.delete(f"{API}/{a}").status_code == 200       # 手动的删也照常放行
    assert len(env.client.get(API).json()["items"]) == 1                    # 手添的那条


def test_pausing_search_does_not_hide_the_memories(env, monkeypatch):
    _seed(env, TEXT_A)
    monkeypatch.setattr(env.settings, "memory_search_enabled", False)

    body = env.client.get(API).json()

    assert body["items"] and body["items"][0]["text"] == TEXT_A    # 列表照常(页面里的检索是用户主动行为)


def test_a_disabled_feature_is_503_but_the_status_stays_readable(env, monkeypatch):
    _seed(env, TEXT_A)
    monkeypatch.setattr(env.settings, "memory_enabled", False)

    refused = env.client.get(API)
    assert refused.status_code == 503 and "MEMORY_ENABLED" in refused.json()["detail"]

    status_body = env.client.get(f"{API}/status").json()
    assert status_body["enabled"] is False and status_body["items"] == 0


def test_unconfigured_mysql_is_503_with_the_reason(env, monkeypatch):
    monkeypatch.setattr(env.settings, "mysql_url", "")

    for response in (env.client.get(API), env.client.post(API, json={"text": "x"}),
                     env.client.delete(API, params={"confirm": True})):
        assert response.status_code == 503
        assert "MYSQL_URL" in response.json()["detail"]


# ---- 结构:user_id 只能来自 Principal ----


def test_no_interface_accepts_a_user_id():
    """§12 的硬要求:没有任何接口能从请求里指定归属。

    遍历 /api/v1/memory 的全部路由,检查查询参数与请求体字段 —— 出现任何形如 user/user_id
    的参数都算破例(真要加,得先把归属规则想清楚,而不是顺手加个参数)。

    路由表遍历复用 test_auth_rbac 的展平函数(include_router 会把子树包一层,直接走
    app.routes 看不到里面)。
    """
    from tests.test_auth_rbac import iter_api_routes

    offenders = []
    checked = 0
    for route in iter_api_routes(app):
        if not route.path.startswith(API):
            continue
        checked += 1
        names = {p.name for p in route.dependant.query_params}
        body = getattr(route, "body_field", None)
        model = getattr(body, "field_info", None)
        if model is not None:
            names |= set(getattr(model.annotation, "model_fields", {}) or {})
        offenders += [f"{route.path}:{n}" for n in names
                      if n == "user" or n.startswith("user_") or n.endswith("_user_id")]
    assert checked >= 6, "没遍历到记忆接口(路由前缀变了?)"
    assert not offenders, "接口不接受用户归属,归属只来自 Principal:" + ",".join(offenders)
