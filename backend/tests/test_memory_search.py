"""混合检索编排(§8):两路召回 → MySQL 校验 → RRF → 重排序 → 预算。

这里验证的是**编排契约**,不是外部服务的真实行为:
- 真表(临时 SQLite)+ 内存 Qdrant + 假 Embeddings + 脚本化的稠密/重排替身;
- 稠密召回的名次由测试脚本给(向量相似度本身不可离线造),所以断言的是
  「拿到名次之后我们做了什么」——融合、校验、降级、预算、顺序归属。

未覆盖:真实 Qdrant 的 HNSW 行为、真实 qwen3.7-text-rerank 的排序质量 ——
那两件事只能在有凭证的环境里验,见技术方案「未验证事项」。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.memory import repo, search
from app.memory.errors import MemoryNotConfigured
from tests import memorykit

U1 = "00000000-0000-4000-8000-000000000001"
U2 = "00000000-0000-4000-8000-000000000002"

TEXT_A = "用户偏好用中文写实验记录"       # 两路都会命中
TEXT_B = "用户喜欢喝咖啡"                 # 只有稠密命中(脚本给)
TEXT_C = "用户每周五整理实验数据"         # 只有 BM25 命中(含「实验」)
QUERY = "中文实验记录"


# 小而确定的检索参数:候选面比最终条数大,便于观察「谁进了最终结果」
SEARCH_PARAMS = {
    "memory_vector_k": 3, "memory_bm25_k": 3, "memory_rerank_k": 4, "memory_top_k": 3,
    "memory_rrf_k": 60, "memory_context_chars": 2000, "memory_bm25_corpus_limit": 1000,
    "memory_rerank_max_docs": 50, "memory_rerank_max_chars": 8000,
    "memory_maintenance_vector_k": 5, "memory_maintenance_bm25_k": 5,
    "memory_maintenance_rerank_k": 5, "memory_maintenance_top_k": 4,
}


@pytest.fixture
def search_env(memory_db, cache_env, monkeypatch):
    """离线检索环境:真表 + 内存 Qdrant + 假 Embeddings + 脚本化稠密/重排(见 memorykit)。"""
    s = memory_db.settings
    env = memorykit.install(monkeypatch, settings=s, dim=s.embeddings_dim, params=SEARCH_PARAMS)
    env.db = memory_db.db
    env.cache = cache_env
    return env


def _seed(env, *texts: str, user_id: str = U1) -> list[str]:
    return memorykit.seed(env, env.db, *texts, user_id=user_id)


def _ids(result) -> list[str]:
    return [h.memory_id for h in result.hits]


def _three(env) -> tuple[str, str, str]:
    a, b, c = _seed(env, TEXT_A, TEXT_B, TEXT_C)
    return a, b, c


# ---- 基本编排:两路召回 → 融合 ----


def test_bm25_only_hit_reaches_the_final_result(search_env):
    """BM25 独有命中必须能进最终候选:否则关键词召回路等于白做。"""
    a, b, c = _three(search_env)
    search_env.dense.ids = [b, a]                  # 稠密只召回 b、a

    result = search.search(U1, QUERY)

    assert result.ok and set(_ids(result)) == {a, b, c}
    assert result.counts["dense"] == 2 and result.counts["keyword"] == 2   # a、c 是关键词命中
    assert result.counts["union"] == 3

    by_id = {h.memory_id: h for h in result.hits}
    assert by_id[c].vector_rank is None and by_id[c].bm25_rank == 2     # 只来自关键词
    assert by_id[b].bm25_rank is None and by_id[b].vector_rank == 1
    assert by_id[a].vector_rank == 2 and by_id[a].bm25_rank == 1        # 两路都命中
    assert _ids(result) == [a, b, c]                                    # a 两路相加最高


def test_dense_results_are_requested_with_the_trusted_user_scope(search_env):
    a, _, _ = _three(search_env)
    search_env.dense.ids = [a]

    search.search(U1, QUERY)

    call = search_env.dense.calls[0]
    s = search_env.settings
    assert call["user_id"] == U1                   # 用户范围来自调用方,不是查询串
    assert call["top_k"] == s.memory_vector_k + s.memory_vector_oversample   # 多取,用于补位
    assert call["vector"]                            # 查询向量由 embeddings 现算


def test_stale_candidates_are_backfilled_up_to_the_configured_count(search_env):
    """旧向量被过滤掉之后,用多取的那部分补上 —— 否则一次换模型就会白掉一大截召回。"""
    stale = _seed(search_env, "用户偏好把结果导出为PDF")[0]
    with search_env.db.session_scope() as session:
        repo.mark_indexed(session, ids=[stale], embedding_version="old@1",
                          now=search_env.db.utc_naive())
    v1, v2, v3 = _seed(search_env, TEXT_A, TEXT_B, TEXT_C)
    search_env.dense.ids = [stale, v1, v2, v3]     # 无效的那条排在最前面

    result = search.search(U1, "完全无关的查询词")

    assert result.counts["dense_raw"] == 4 and result.counts["dense"] == 3   # 补位后仍是 3 条
    assert result.counts["filtered_stale"] == 1
    assert any("索引落后" in note for note in result.degraded)
    by_id = {h.memory_id: h for h in result.hits}
    assert by_id[v1].vector_rank == 2              # 名次沿用召回时的真实名次(不去填补空位)
    assert stale not in by_id


def test_dense_candidates_are_capped_after_validation(search_env, monkeypatch):
    """补取有上限:多取的那些只在补位时用,采纳条数仍以 vector_k 为准。"""
    ids = _seed(search_env, TEXT_A, TEXT_B, TEXT_C, "用户在做报销流程优化", "用户的学号是 12345")
    monkeypatch.setattr(search_env.settings, "memory_vector_k", 3)
    monkeypatch.setattr(search_env.settings, "memory_vector_oversample", 2)
    search_env.dense.ids = list(ids)               # 返回 5 条,但只能采纳 3 条

    result = search.search(U1, QUERY)

    assert search_env.dense.calls[0]["top_k"] == 5
    assert result.counts["dense_raw"] == 5 and result.counts["dense"] == 3


def test_other_users_memories_are_never_returned(search_env):
    """召回层按用户过滤,MySQL 侧再按 user_id 确认一次:Qdrant 过滤条件配错也不越权。"""
    mine = _seed(search_env, TEXT_A, user_id=U1)
    theirs = _seed(search_env, TEXT_A, user_id=U2)
    search_env.dense.ids = [*mine, *theirs]        # 假装过滤失效,把别人的也召回

    result = search.search(U1, QUERY)

    assert _ids(result) == list(mine)
    assert result.counts["filtered_foreign"] == 1
    assert theirs[0] not in _ids(result)


def test_bm25_corpus_is_scoped_to_one_user(search_env):
    mine = _seed(search_env, TEXT_A, user_id=U1)
    _seed(search_env, TEXT_C, user_id=U2)
    search_env.dense.fail = RuntimeError("qdrant 不可达")

    result = search.search(U1, QUERY)

    assert _ids(result) == list(mine)


# ---- 校验:状态与索引版本 ----


def test_rows_whose_vectors_are_stale_are_filtered_out(search_env):
    """换过 Embedding 模型就会留下旧向量:那种"语义相似"不可信,排除并计数。"""
    stale_id = _seed(search_env, "用户偏好把结果导出为PDF")[0]
    with search_env.db.session_scope() as session:
        repo.mark_indexed(session, ids=[stale_id], embedding_version="old@1",
                          now=search_env.db.utc_naive())
    search_env.dense.ids = [stale_id]

    result = search.search(U1, "完全无关的查询词")

    assert result.hits == [] and result.counts["filtered_stale"] == 1


def test_deleted_rows_disappear_even_if_the_index_still_has_them(search_env):
    gone = _seed(search_env, TEXT_A)[0]
    with search_env.db.session_scope() as session:
        repo.delete_item(session, user_id=U1, memory_id=gone, actor="user",
                         now=search_env.db.utc_naive())
    search_env.dense.ids = [gone]

    result = search.search(U1, QUERY)

    assert gone not in _ids(result) and result.counts["filtered_missing"] == 1


def test_dense_candidates_are_deduplicated(search_env):
    a, _, _ = _three(search_env)
    search_env.dense.ids = [a, a, a]               # 点重复出现不该重复参与融合

    result = search.search(U1, QUERY)

    assert result.counts["dense"] == 1
    assert _ids(result).count(a) == 1


def test_index_points_from_another_revision_are_not_evidence(search_env):
    """索引里残留旧版本的点(正文改过而点没重建):不能拿它的相似度当证据。

    这正是「MySQL 已经是 revision 2、Qdrant 还停在 1」那种坏状态:命中看起来完全正常
    (归属对、作用域对、词也对),唯一的破绽就是 payload 里的版本与当前事实对不上。
    """
    a = _unindexed("用户偏好把结果导出为PDF", search_env)   # 关键词查不到它,只看向量那一跳
    search_env.dense.ids = [a]
    search_env.dense.skew = {a: {"revision": 0}}           # 点还标着第 0 版

    result = search.search(U1, "完全无关的查询词")

    assert result.hits == [] and result.counts["filtered_stale"] == 1


def test_index_points_with_an_old_content_hash_are_not_evidence(search_env):
    """正文摘要对不上:同样不作数(payload 里的 content_hash 与当前正文不一致)。"""
    a = _unindexed("用户偏好把结果导出为PDF", search_env)
    search_env.dense.ids = [a]
    search_env.dense.skew = {a: {"content_hash": "0" * 32}}

    result = search.search(U1, "完全无关的查询词")

    assert result.hits == [] and result.counts["filtered_stale"] == 1


# ---- 作用域隔离:正式数据与评测数据在检索层也不许混 ----

EVAL_SCOPE = "eval:run-1"


def test_eval_scope_memories_never_reach_the_formal_search(search_env):
    """即便索引层把作用域过滤搞错(把评测点也召回了),MySQL 侧也要挡下来。"""
    formal = _seed(search_env, TEXT_A)[0]
    leaked = memorykit.seed(search_env, search_env.db, TEXT_C, user_id=U1,
                            scope=EVAL_SCOPE)[0]
    search_env.dense.ids = [leaked, formal]         # 假装 Qdrant 的过滤条件没生效

    result = search.search(U1, QUERY, scope="")

    assert _ids(result) == [formal]
    assert result.counts["filtered_scope"] == 1
    assert leaked not in _ids(result)


def test_formal_memories_never_reach_an_eval_search(search_env):
    """反向同样成立:评测检索只看得见自己那一个 run 的记忆。"""
    _seed(search_env, TEXT_A)
    mine = memorykit.seed(search_env, search_env.db, TEXT_C, user_id=U1,
                          scope=EVAL_SCOPE)[0]

    result = search.search(U1, QUERY, scope=EVAL_SCOPE)

    assert _ids(result) == [mine]


def _unindexed(text: str, env, user_id: str = U1) -> str:
    """落一条**事实已成立、索引还没建**的记忆:模拟「刚写完 / 索引失败待重放」。

    直接走 repo(不经过 service.write_facts),所以 Qdrant 里根本没有它的点 —— 这正是
    索引落后时检索侧看到的样子。
    """
    with env.db.session_scope() as session:
        return repo.create_item(session, user_id=user_id, text=text,
                                now=env.db.utc_naive()).id


def test_keyword_recall_does_not_depend_on_the_vector_index(search_env):
    """向量索引还没跟上的记忆,关键词通道照样召回(§8:BM25 不依赖向量索引状态)。

    构造:一条记忆事实已落库、点还没写进 Qdrant —— 稀疏索引里没有它,向量那一跳也没有,
    但它的正文就在 MySQL 里(权威)。补进来的候选只依据正文,因此仍然可召回,
    并把「索引尚未同步」如实记进降级说明。
    """
    _seed(search_env, TEXT_A)                       # 已建好索引的正常记忆(集合因此存在)
    mid = _unindexed(TEXT_C, search_env)            # 这一条只有事实、没有点
    search_env.dense.ids = []                       # 向量这一路也没有它

    result = search.search(U1, "实验数据")

    assert mid in _ids(result)
    assert result.counts["keyword_supplement"] == 1      # 靠正文补进来的那一条
    assert any("索引尚未同步" in note for note in result.degraded), result.degraded


def test_sparse_channel_outage_degrades_to_in_memory_bm25(search_env, monkeypatch):
    """稀疏通道不可用时的降级路径:用进程内 BM25,并且**说出来**(不装作一切正常)。"""
    a, _b, c = _three(search_env)
    memorykit.sparse_missing(monkeypatch)

    result = search.search(U1, QUERY)

    assert result.counts["keyword_channel_sparse"] == 0        # 确实没走稀疏索引
    assert any("稀疏检索通道不可用" in note for note in result.degraded)
    assert set(_ids(result)) == {a, c}                         # b 没有关键词命中


def test_bm25_corpus_cap_is_reported_with_coverage(search_env, monkeypatch):
    """降级通道只读最近 N 条时必须说明「覆盖了多少 / 共多少」,不能静默少检。"""
    _seed(search_env, TEXT_A, TEXT_C, "用户在实验里用表格记数据")
    memorykit.sparse_missing(monkeypatch)
    monkeypatch.setattr(search_env.settings, "memory_bm25_corpus_limit", 2)

    result = search.search(U1, "实验")

    note = next(n for n in result.degraded if "语料达到上限" in n)
    assert "覆盖 2 / 共 3 条" in note


# ---- RRF:只合并名次,不合并分数 ----


def test_rrf_uses_reciprocal_ranks_only():
    fused = search._rrf([("a", 1, 1), ("b", 2, None)], k=60)

    assert fused[0][0] == "a"
    assert fused[0][1] == pytest.approx(1 / 61 + 1 / 61)
    assert fused[1][1] == pytest.approx(1 / 62)    # 只有一路命中,贡献一份


def test_bm25_score_magnitude_does_not_change_the_fusion(search_env, monkeypatch):
    """把 BM25 分数整体放大 1e9 倍(名次不变),融合结果必须一模一样。

    直接相加未归一化的 BM25 与余弦分数会在这里翻车 —— RRF 只吃名次。
    这里把稀疏通道关掉,考察的就是降级通道(进程内 BM25)的分数口径。
    """
    a, b, c = _three(search_env)
    search_env.dense.ids = [b, a]
    memorykit.sparse_missing(monkeypatch)
    baseline = _ids(search.search(U1, QUERY))

    original = search.memory_text.bm25_scores

    def inflated(query, docs, **kwargs):
        return [SimpleNamespace(doc_id=h.doc_id, score=h.score * 1e9, matched=h.matched)
                for h in original(query, docs, **kwargs)]

    monkeypatch.setattr(search.memory_text, "bm25_scores", inflated)
    assert _ids(search.search(U1, QUERY)) == baseline == [a, b, c]


def test_rrf_k_comes_from_settings(search_env, monkeypatch):
    a, _, _ = _three(search_env)
    search_env.dense.ids = [a]

    monkeypatch.setattr(search_env.settings, "memory_rrf_k", 1)
    hit = search.search(U1, QUERY).hits[0]

    assert hit.rrf == pytest.approx(2 / 2)          # 两路都排第一:1/(1+1) × 2


# ---- 重排序:保持模型顺序,失败回退 ----


def test_rerank_order_wins_and_unreturned_candidates_follow_in_fusion_order(search_env):
    a, b, c = _three(search_env)
    search_env.dense.ids = [b, a]                  # 融合顺序是 a, b, c
    search_env.rerank.enabled = True
    search_env.rerank.order = [1, 0]               # 模型把 b 排到 a 前面
    search_env.rerank.scores = {1: 0.93, 0: 0.11}

    result = search.search(U1, QUERY)

    assert _ids(result) == [b, a, c]               # 重排之后不再被 RRF 覆盖
    assert result.hits[0].origin == "rerank" and result.hits[0].score == 0.93
    assert result.hits[2].origin == "rrf"          # 模型没返回的按融合顺序续尾
    assert result.hits[2].rrf > 0                  # 重排过也不丢 RRF 诊断分
    assert result.counts["rerank_out"] == 2
    assert result.degraded == []


def test_opt_in_trace_connects_fusion_rerank_and_final_without_body(search_env):
    a, b, c = _three(search_env)
    search_env.dense.ids = [b, a]
    search_env.rerank.enabled = True
    search_env.rerank.order = [1, 0]
    search_env.rerank.scores = {1: 0.93, 0: 0.11}
    result = search.search(U1, QUERY, capture_trace=True)
    assert {r["memory_id"] for r in result.trace["validated"]} == {a, b, c}
    assert result.trace["rerank_input"] == [r["memory_id"] for r in result.trace["fused"]]
    assert result.trace["rerank_returned"][0]["score"] == 0.93
    assert result.trace["final"] == _ids(result)
    assert search.search(U1, QUERY).trace == {}
    assert "text" not in str(result.trace)


def test_rerank_receives_the_fused_head_in_order(search_env):
    a, b, c = _three(search_env)
    search_env.dense.ids = [b, a]
    search_env.rerank.enabled = True

    search.search(U1, QUERY)

    call = search_env.rerank.calls[0]
    assert call["query"] == QUERY
    assert call["documents"] == [TEXT_A, TEXT_B, TEXT_C]     # 融合顺序
    assert call["top_n"] == 3                                # 不是最终 top_k 那种小数字
    assert call["instruct"] == ""                            # 回答检索:用供应商默认提示


def test_rerank_failure_falls_back_to_fusion_and_says_so(search_env):
    a, b, c = _three(search_env)
    search_env.dense.ids = [b, a]
    search_env.rerank.enabled = True
    search_env.rerank.fail = "HTTP 500(InternalError)"

    result = search.search(U1, QUERY)

    assert _ids(result) == [a, b, c] and all(h.origin == "rrf" for h in result.hits)
    assert any("重排序失败" in note and "HTTP 500" in note for note in result.degraded)


def test_rerank_exception_never_breaks_the_search(search_env):
    a, _b, _c = _three(search_env)
    search_env.dense.ids = [a]
    search_env.rerank.enabled = True
    search_env.rerank.raise_ = RuntimeError("客户端炸了")

    result = search.search(U1, QUERY)

    assert result.ok and _ids(result)[0] == a
    assert any("重排序失败" in note for note in result.degraded)


def test_rerank_input_is_capped_and_reported(search_env, monkeypatch):
    a, b, c = _three(search_env)
    search_env.dense.ids = [b, a]
    search_env.rerank.enabled = True
    monkeypatch.setattr(search_env.settings, "memory_rerank_max_docs", 2)

    result = search.search(U1, QUERY)

    assert search_env.rerank.calls[0]["documents"] == [TEXT_A, TEXT_B]   # 只送前两条
    assert any("重排候选超出配置上限" in note for note in result.degraded)
    assert _ids(result) == [a, b, c]                                     # 没送的仍按融合顺序在


def test_rerank_is_skipped_when_not_configured(search_env):
    a, _, _ = _three(search_env)
    search_env.dense.ids = [a]

    result = search.search(U1, QUERY)

    assert search_env.rerank.calls == []           # 没配置就不发请求
    assert any("重排序已关闭" in note for note in result.degraded)


# ---- 降级:向量不可用时用 BM25,两路都不行才是失败 ----


def test_dense_failure_degrades_to_keyword_only(search_env):
    a, _b, c = _three(search_env)
    search_env.dense.fail = RuntimeError("qdrant 不可达")

    result = search.search(U1, QUERY)

    assert result.ok and set(_ids(result)) == {a, c}          # b 只来自稠密,自然缺席
    assert any("向量召回不可用" in note and "RuntimeError" in note
               for note in result.degraded)
    assert result.counts["dense"] == 0


def test_embedding_endpoint_down_also_degrades_to_keyword_only(search_env, monkeypatch):
    a, _b, c = _three(search_env)

    def boom():
        raise RuntimeError("embedding 端点不可达")

    monkeypatch.setattr(search, "get_embeddings", boom)
    result = search.search(U1, QUERY)

    assert result.ok and set(_ids(result)) == {a, c}
    assert any("向量召回不可用" in note for note in result.degraded)


def test_empty_keyword_result_is_not_an_error(search_env, monkeypatch):
    """「跑了但没命中」是正常结果,不能报成故障 —— 否则聊天会以为记忆坏了。"""
    _three(search_env)
    search_env.dense.fail = RuntimeError("qdrant 不可达")

    result = search.search(U1, "与所有记忆都无关的查询词")

    assert result.ok and result.hits == [] and result.error == ""
    assert any("向量召回不可用" in note for note in result.degraded)


def test_both_channels_down_reports_an_error_not_silence(search_env, monkeypatch):
    _three(search_env)
    search_env.dense.fail = MemoryNotConfigured("没配 QDRANT_URL")
    memorykit.sparse_missing(monkeypatch)          # 稀疏通道也不在:降级到进程内 BM25

    def boom(*args, **kwargs):
        raise RuntimeError("mysql 不可达")

    monkeypatch.setattr(repo, "active_texts", boom)
    result = search.search(U1, QUERY)

    assert not result.ok and result.hits == []
    assert "记忆检索不可用" in result.error


def test_missing_user_id_is_rejected_before_any_recall(search_env):
    result = search.search("", QUERY)

    assert not result.ok and result.error == "缺少用户标识"
    assert search_env.dense.calls == []            # 没有用户就没有查询,不猜


def test_empty_query_makes_no_calls(search_env):
    result = search.search(U1, "   ")

    assert result.hits == [] and result.error == ""
    assert search_env.dense.calls == [] and search_env.rerank.calls == []


# ---- 上下文预算 ----


def test_context_budget_truncates_and_marks_it(search_env, monkeypatch):
    a, b, c = _three(search_env)
    search_env.dense.ids = [b, a]
    monkeypatch.setattr(search_env.settings, "memory_context_chars", 8)

    result = search.search(U1, QUERY)

    assert _ids(result) == [a]                     # 预算只够第一条(且只够前 8 个字)
    assert result.hits[0].trimmed and len(result.hits[0].text) == 8
    assert any("上下文预算" in note for note in result.degraded)


def test_result_within_budget_is_not_trimmed(search_env):
    a, _, _ = _three(search_env)
    search_env.dense.ids = [a]

    hit = search.search(U1, QUERY).hits[0]

    assert hit.text == TEXT_A and not hit.trimmed


# ---- 参数档位:回答检索 vs 维护决策 ----


def test_maintenance_profile_is_wider_and_carries_the_instruct(search_env):
    a, _, _ = _three(search_env)
    search_env.dense.ids = [a]
    search_env.rerank.enabled = True

    profile = search.profile_for(search.TASK_MAINTENANCE)
    assert (profile.vector_k, profile.bm25_k, profile.rerank_k, profile.top_k) == (5, 5, 5, 4)
    assert profile.rerank_instruct == search_env.settings.memory_rerank_instruct

    search.search(U1, QUERY, task=search.TASK_MAINTENANCE)

    assert search_env.rerank.calls[0]["instruct"] == search_env.settings.memory_rerank_instruct
    assert search_env.dense.calls[0]["top_k"] == 5 + search_env.settings.memory_vector_oversample


def test_unknown_task_falls_back_to_the_answer_profile(search_env):
    assert search.profile_for("something-else").top_k == search_env.settings.memory_top_k


def test_top_k_argument_overrides_the_profile(search_env):
    a, b, c = _three(search_env)
    search_env.dense.ids = [b, a]

    result = search.search(U1, QUERY, top_k=1)

    assert len(result.hits) == 1


# ---- 证据块 ----


def test_format_evidence_labels_dates_and_truncation(search_env, monkeypatch):
    a, _, _ = _three(search_env)
    search_env.dense.ids = [a]
    monkeypatch.setattr(search_env.settings, "memory_context_chars", 8)
    result = search.search(U1, QUERY)

    block = search.format_evidence(result)

    assert block.startswith("[记忆 1｜") and "(截断)" in block
    assert TEXT_A[:8] in block and TEXT_A not in block


def test_format_evidence_is_empty_without_hits():
    assert search.format_evidence(search.SearchResult(query="x")) == ""


def test_settings_have_rerank_defaults_that_are_off():
    """默认不启用重排序:没配 Key 的环境不该偷偷调外部服务。"""
    s = get_settings()
    assert isinstance(s.rerank_enabled, bool) and s.memory_rrf_k > 0
    assert s.memory_top_k <= s.memory_rerank_k <= s.memory_vector_k + s.memory_bm25_k
