"""Graph recall participates in the existing search and fails safely."""
from tests.test_memory_search import search_env, _seed, U1, QUERY
from app.memory import search, db, repo
from app.memory.graph import runtime


def test_graph_only_candidate_is_authoritatively_validated(search_env, monkeypatch):
    mid = _seed(search_env, "独立的关联事实")[0]
    with db.session_scope() as session:
        item = repo.get_item(session, U1, mid)
    monkeypatch.setattr(runtime, "recall", lambda **kw: {
        "enabled": True, "candidates": [mid], "paths": [],
        "versions": {mid: {k: getattr(item, k) for k in
            ("revision", "meta_version", "generation", "content_hash")}}})
    result = search.search(U1, QUERY)
    assert [h.memory_id for h in result.hits] == [mid]
    assert result.counts["keyword"] == 0


def test_graph_outage_preserves_two_route_results(search_env, monkeypatch):
    mid = _seed(search_env, "中文实验记录")[0]
    def fail(**kwargs):
        raise RuntimeError("offline")
    monkeypatch.setattr(runtime, "recall", fail)
    result = search.search(U1, QUERY)
    assert mid in [h.memory_id for h in result.hits]
    assert any("图召回不可用" in n for n in result.degraded)


def test_disabled_graph_never_reports_path_budget(search_env):
    _seed(search_env, "中文实验记录")
    result = search.search(U1, QUERY, top_k=1)
    assert not any("图证据组合" in n for n in result.degraded)


def test_graph_supplements_without_evicting_existing_rerank_pool(search_env, monkeypatch):
    base = _seed(search_env, "第一事实", "第二事实", "第三事实")
    extra = _seed(search_env, "新增事实甲", "新增事实乙", "新增事实丙")
    search_env.dense.ids = base
    search_env.rerank.enabled = True
    with db.session_scope() as session:
        items = repo.list_by_ids(session, extra)
    monkeypatch.setattr(runtime, "recall", lambda **kw: {
        "enabled": True, "candidates": extra, "paths": [],
        "versions": {x.id: {k: getattr(x, k) for k in
            ("revision", "meta_version", "generation", "content_hash")} for x in items}})
    result = search.search(U1, "不匹配的查询", capture_trace=True)
    assert set(result.trace["base_rerank_pool"]) == set(base)
    assert set(result.trace["rerank_input"]) == set(base + extra)
    assert search_env.rerank.calls[-1]["top_n"] == 6
    monkeypatch.setattr(search_env.settings, "memory_rerank_max_docs", 3)
    result = search.search(U1, "不匹配的查询", capture_trace=True)
    assert set(result.trace["rerank_input"]) == set(base)


def test_stale_graph_candidate_is_not_used(search_env, monkeypatch):
    mid = _seed(search_env, "独立的关联事实")[0]
    monkeypatch.setattr(runtime, "recall", lambda **kw: {
        "enabled": True, "candidates": [mid], "paths": [], "versions": {mid: {
            "revision": -1, "meta_version": -1, "generation": -1, "content_hash": "old"}}})
    assert not search.search(U1, QUERY).hits


def test_multi_fact_path_is_kept_whole_or_rejected(search_env, monkeypatch):
    ids = _seed(search_env, "甲参与乙项目", "乙项目使用丙技术")
    with db.session_scope() as session:
        items = repo.list_by_ids(session, ids)
    monkeypatch.setattr(runtime, "recall", lambda **kw: {
        "enabled": True, "candidates": ids, "paths": [{"memory_ids": ids}],
        "versions": {x.id: {k: getattr(x, k) for k in
            ("revision", "meta_version", "generation", "content_hash")} for x in items}})
    assert set(h.memory_id for h in search.search(U1, "无关键词", top_k=2).hits) == set(ids)
    result = search.search(U1, "无关键词", top_k=1)
    assert not result.hits
    assert result.degraded
