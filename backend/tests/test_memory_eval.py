"""评测入口(`scripts/memory_eval.py`)的离线自检(§14)。

这里断言的是**评测流程本身**:身份与索引隔离、构建阶段不读问题、三种模式导出、
调用统计的取数口径、清理只清自己那一份。检索与提取都换成替身(见 memorykit),
所以**不涉及任何真实付费调用**,也不构成对真实 MySQL / Qdrant / 模型效果的验证 ——
效果要另外在配齐凭证的独占环境里小样本跑(见 docs/长期记忆系统部署与评测指南.md)。

两条测试约定值得写明:
- 维护决策**关掉**(MEMORY_MAINTENANCE_ENABLED=false):构建期只走「提取 → 写入」一条路,
  提取脚本才不会被维护决策那次调用吃掉,结果可复现(评测要固定生成参数与提示词)。
- 假提取模型按脚本给事实,**内容与数据集里的会话一一对应**,于是「谁被记住」由测试安排,
  不是由模型决定。
"""

from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.memory import repo, service
from scripts import memory_eval as me
from tests import memorykit

# 检索参数压小:小样本下「谁进了最终结果」一眼能看清
PARAMS = {
    "memory_vector_k": 3, "memory_bm25_k": 3, "memory_rerank_k": 3, "memory_top_k": 3,
    "memory_rrf_k": 60, "memory_context_chars": 2000, "memory_bm25_corpus_limit": 1000,
    "memory_rerank_max_docs": 50, "memory_rerank_max_chars": 8000,
}

CONVERSATIONS = [
    {"id": "c1", "turns": [{"user": "我习惯用中文写实验记录", "assistant": "明白,以后用中文。"},
                           {"user": "我每周五整理实验数据", "assistant": "记下了。"}]},
    {"id": "c2", "turns": [{"user": "我报销时把发票按项目分摊", "assistant": "好的。"}]},
]
FACTS = ["用户习惯用中文写实验记录", "用户每周五整理实验数据", "用户报销时把发票按项目分摊"]
QUESTIONS = [{"id": "q1", "conversation_id": "c1", "question": "我写实验记录用什么语言?",
              "answer": "中文", "category": "preference"}]


def payload(*, conversations=None, questions=None) -> dict:
    return {"name": "test-sample", "conversations": conversations or CONVERSATIONS,
            "questions": questions or QUESTIONS}


@pytest.fixture
def env(memory_db, cache_env, monkeypatch, tmp_path):
    """离线评测环境:真记忆表(SQLite)+ 内存 Qdrant + 假 Embeddings / 提取模型 / 聊天模型。

    与 test_memory_chat.py 同一套装配思路,额外把评测脚本的三处外部接口换成替身:
    运行目录(不写进仓库的 data/)、记忆模型、聊天模型。
    """
    s = get_settings()
    # 关维护决策:构建期只走提取 → 写入,提取脚本不被维护那次调用吃掉(见模块 docstring)
    monkeypatch.setattr(s, "memory_maintenance_enabled", False)
    monkeypatch.setattr(s, "memory_write_enabled", True)
    monkeypatch.setattr(s, "memory_search_enabled", True)
    prod_collection = s.memory_collection
    monkeypatch.setattr(s, "memory_collection", prod_collection)   # 记录原值,用完还原
    monkeypatch.setattr(me, "RUNS_DIR", tmp_path / "runs")

    kit = memorykit.install(monkeypatch, settings=s, dim=s.embeddings_dim, params=PARAMS)
    kit.db = memory_db.db
    kit.settings = s
    kit.prod_collection = prod_collection
    kit.memory_llm = memorykit.FakeLLM()            # 提取:由测试按顺序喂 JSON
    kit.chat_llm = memorykit.FakeLLM("这是模型的回答。")
    monkeypatch.setattr("app.memory.llm.get_memory_llm", lambda: kit.memory_llm)
    monkeypatch.setattr("app.llm.get_llm", lambda: kit.chat_llm)

    yield kit
    memory_db.db.dispose()


def write_dataset(tmp_path, data: dict) -> dict:
    """落盘再加载:走真实的 load_dataset(含校验与数据集哈希)。"""
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return me.load_dataset(str(path))


def script_extraction(env, texts: list[str]) -> None:
    """按顺序给提取模型一串「一条事实」的输出,与数据集里的会话一一对应。"""
    env.memory_llm.script = [json.dumps({"facts": [{"text": t, "kind": "preference"}]},
                                        ensure_ascii=False) for t in texts]


def memory_texts(user_id: str, scope: str = "") -> list[str]:
    """库里该用户在该作用域内的记忆正文(按更新时间倒序,与列表页同口径)。"""
    return [item.text for item in
            service.list_items(user_id=user_id, limit=100, scope=scope)["items"]]


def run_texts(run) -> list[str]:
    """本次评测那一份记忆的正文(作用域是它的,不是正式那一份)。"""
    return memory_texts(run.user_id, run.scope)


# ---------------------------------------------------------------- 隔离


def test_identity_and_scope_are_isolated(env, tmp_path):
    """评测身份与作用域都另起一套:同一 run-id 稳定,且不改动本进程的索引设置。"""
    assert me.eval_user_id("run-a") == me.eval_user_id("run-a")
    assert me.eval_user_id("run-a") != me.eval_user_id("run-b")
    assert me.eval_user_id("run-a") == me.eval_user_id("run-a", "")      # 缺省主体 = 整 run 一个用户
    assert me.eval_user_id("run-a", "alice") != me.eval_user_id("run-a", "bob")

    run = me.EvalEnv("run-a")
    assert run.scope == "eval:run-a" and run.scope != ""     # 正式作用域是空串
    assert get_settings().memory_collection == env.prod_collection   # 不换集合:靠 scope 隔离
    assert run.collection == env.prod_collection
    assert run.dir.exists()                             # 运行目录已建,且落在临时目录里
    assert "run-a" in run.dir.name


def test_run_id_is_validated_before_it_becomes_a_path_or_a_scope(env):
    """run-id 既是作用域又是目录名:空的、带路径分隔符的、带空格的都拒绝。"""
    for bad in ("", "   ", "../escape", "a/b", "with space", "太长了" * 20):
        with pytest.raises(SystemExit):
            me.check_run_id(bad)

    assert me.check_run_id(" smoke-0001 ") == "smoke-0001"   # 首尾空白去掉后照常收


# ---------------------------------------------------------------- 构建


def test_build_imports_conversations_in_order_and_drains(env, tmp_path):
    """按顺序登记每一轮对话,驱动 worker 跑到队列与索引都清空。"""
    dataset = write_dataset(tmp_path, payload())
    script_extraction(env, FACTS)
    run = me.EvalEnv("build-ok")

    summary = me.build(run, dataset, max_ticks=50)

    assert summary["exchanges"] == 3 and summary["jobs_queued"] == 3
    assert summary["drained"] is True and summary["index_pending"] == 0
    assert summary["memories"] == 3
    assert run_texts(run) == list(reversed(FACTS))    # 倒序:最新的一条在前
    # 每条事实都真进了评测 collection(不是说说而已)
    from app.memory import vector
    assert vector.count(user_id=run.user_id) == 3

    # 构建期**不做检索**:一次稠密召回都没有(不该为评测问题提前花钱)
    assert env.dense.calls == []
    # 提取模型逐条看到的是**当轮**会话,顺序与数据集一致(不许把后面的轮次提前喂进去)
    prompts = env.memory_llm.prompts
    assert len(prompts) == 3
    assert "我习惯用中文写实验记录" in prompts[0]
    assert "发票按项目分摊" in prompts[2] and "发票按项目分摊" not in prompts[0]
    # build.json 落盘(人工复核时要能对上)
    assert json.loads(run.path("build.json").read_text(encoding="utf-8"))["memories"] == 3


def test_build_stops_when_worker_never_drains(env, tmp_path, monkeypatch):
    """跑不完就如实报「没清空」,不假装成功(CLI 据此返回非 0)。"""

    class _Stuck:
        """替身:认领了任务却什么都不做(模拟后台一直没跟上)。"""

        def __init__(self, **_kwargs):
            pass

        def run_once(self, **kwargs):
            return {}

    monkeypatch.setattr("app.memory.worker.MemoryWorker", _Stuck)
    dataset = write_dataset(tmp_path, payload(conversations=[
        {"id": "c1", "turns": [{"user": "我习惯用中文写实验记录", "assistant": "明白。"}]}]))
    script_extraction(env, FACTS)
    run = me.EvalEnv("build-stuck")

    summary = me.build(run, dataset, max_ticks=2)

    assert summary["drained"] is False and summary["ticks"] == 2
    assert summary["jobs"]["pending"] == 1          # 任务还排着,没被谁悄悄丢掉
    assert run_texts(run) == []


def test_build_refuses_when_question_text_leaks_into_conversation(env, tmp_path):
    """构建阶段不读问题:会话里混进问题原文时直接拒绝(防呆,不是靠人记得)。"""
    leak = "我写实验记录用什么语言?"
    dataset = write_dataset(tmp_path, payload(conversations=[
        {"id": "c1", "turns": [{"user": f"顺便说下,{leak}", "assistant": "好的。"}]}]))
    run = me.EvalEnv("build-leak")
    script_extraction(env, FACTS)

    with pytest.raises(SystemExit):
        me.build(run, dataset, max_ticks=5)
    assert run_texts(run) == []          # 拒绝得早:一条都没写进去


def test_shipped_sample_dataset_is_loadable(env):
    """随仓库带的冒烟样本要一直能跑:结构变了 / 会话里混进问题原文,这条先炸。"""
    from pathlib import Path

    path = Path(me.__file__).parent / "memory_eval.sample.json"
    dataset = me.load_dataset(str(path))

    exchanges = me.exchanges(dataset["conversations"])
    assert len(exchanges) == 8 and len(dataset["questions"]) == 4
    for ex in exchanges:                                        # 样本自己也得守「构建期不读问题」
        me.assert_no_leak(dataset, [{"content": ex["user"]}, {"content": ex["assistant"]}])


def test_dataset_loader_rejects_incomplete_rows(env, tmp_path):
    """数据集结构不对就报错,不让评测带着半份数据往下跑。"""
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"conversations": [{"id": "c1", "turns": [{"user": "只有提问"}]}]}),
                    encoding="utf-8")
    with pytest.raises(SystemExit):
        me.load_dataset(str(path))


# ---------------------------------------------------------------- 提问与导出


def test_ask_exports_three_modes_without_any_model_call(env, tmp_path):
    """不带 --answer:三种模式的上下文与证据都导出,但一次模型调用都不发。"""
    dataset = write_dataset(tmp_path, payload())
    script_extraction(env, FACTS)
    run = me.EvalEnv("ask-offline")
    me.build(run, dataset, max_ticks=50)
    env.dense.ids = [item.id for item in
                    service.list_items(user_id=run.user_id, limit=100, scope=run.scope)["items"]]

    out = me.ask(run, dataset, modes=["no_memory", "memory", "full_context"], answer=False)

    assert out["answer_calls"] is False
    assert env.chat_llm.calls == []                 # 零模型调用:默认不花钱
    row = out["questions"][0]
    assert set(row["modes"]) == {"no_memory", "memory", "full_context"}
    assert row["modes"]["no_memory"]["context_chars"] == 0
    # 实际参考块与逐条证据一起留档，便于复核模型究竟收到了什么。
    assert me.service.CONTEXT_HEADER in row["modes"]["memory"]["context"]
    evidence = row["modes"]["memory"]["evidence"]
    assert evidence and {"memory_id", "text", "score", "origin", "vector_rank", "bm25_rank"} <= set(
        evidence[0])
    memory_mode = row["modes"]["memory"]
    assert memory_mode["error"] == ""
    # 重排序没配是**配置**不是故障:如实记进 degraded(RRF 兜底),一并写进结果文件
    assert memory_mode["degraded"] == [env.rerank.config_note()]
    # 全量历史:带上整段会话原文,并如实标注是否超出窗口(不静默截断)
    full = row["modes"]["full_context"]
    assert full["history_scope"] == "question_conversation" and full["over_window"] is False
    assert full["history_chars"] > 0 and full["truncated"] is False
    assert json.loads(run.path("results.json").read_text(encoding="utf-8"))["questions"]


def test_recall_block_is_not_in_export_but_evidence_is(env, tmp_path):
    """导出的是证据(可复核),不是拼好的参考块 —— 参考块只是提示词的一部分。"""
    dataset = write_dataset(tmp_path, payload())
    script_extraction(env, FACTS)
    run = me.EvalEnv("ask-evidence")
    me.build(run, dataset, max_ticks=50)
    env.dense.ids = [item.id for item in
                    service.list_items(user_id=run.user_id, limit=100, scope=run.scope)["items"]]

    recall = me._recall(run.user_id, "我写实验记录用什么语言?", scope=run.scope)

    assert me.service.CONTEXT_HEADER in recall["text"]
    assert "参考数据" in recall["text"]              # 与聊天路径同一套护栏文案
    assert recall["hits"] and recall["hits"][0]["memory_id"]
    assert recall["error"] == ""


def test_ask_with_answer_injects_memory_and_calls_once_per_mode(env, tmp_path):
    """--answer:每种模式各一次生成调用;记忆模式的系统提示里带上召回到的事实。"""
    dataset = write_dataset(tmp_path, payload())
    script_extraction(env, FACTS)
    run = me.EvalEnv("ask-answer")
    me.build(run, dataset, max_ticks=50)
    env.dense.ids = [item.id for item in
                    service.list_items(user_id=run.user_id, limit=100, scope=run.scope)["items"]]

    out = me.ask(run, dataset, modes=["no_memory", "memory", "full_context"], answer=True)

    assert out["answer_calls"] is True
    assert len(env.chat_llm.calls) == 3             # 1 条问题 × 3 模式
    systems = [call[0].content for call in env.chat_llm.calls]
    assert "用户习惯用中文写实验记录" in systems[1] and "参考数据" in systems[1]
    assert "用户习惯用中文写实验记录" not in systems[0]        # 基线不注入任何历史
    assert "我习惯用中文写实验记录" in systems[2]              # 全量历史带的是会话原文
    assert out["questions"][0]["modes"]["memory"]["answer"] == "这是模型的回答。"


def test_full_context_over_window_is_recorded_separately(env, tmp_path):
    """全量历史超出窗口的情况单独记录:标注超窗,但**不**悄悄截断成「看起来很合适」。"""
    dataset = write_dataset(tmp_path, payload())
    script_extraction(env, FACTS)
    run = me.EvalEnv("ask-over")
    me.build(run, dataset, max_ticks=50)

    out = me.ask(run, dataset, modes=["full_context"], answer=False, context_chars=10)

    full = out["questions"][0]["modes"]["full_context"]
    assert full["over_window"] is True and full["truncated"] is False
    assert full["history_chars"] > 10


def test_ask_degrades_but_still_exports_when_recall_breaks(env, tmp_path, monkeypatch):
    """检索降级如实记进 degraded,不冒充「你没有记忆」;两路全挂才记 error。"""
    dataset = write_dataset(tmp_path, payload())
    script_extraction(env, FACTS)
    run = me.EvalEnv("ask-broken")
    me.build(run, dataset, max_ticks=50)
    env.dense.ids = [item.id for item in
                    service.list_items(user_id=run.user_id, limit=100, scope=run.scope)["items"]]

    # 1) 稠密一路挂:关键词一路照常给结果 —— 这是设计好的降级(degraded 里说清原因)
    env.dense.fail = RuntimeError("向量库连不上")
    out = me.ask(run, dataset, modes=["memory", "no_memory"], answer=False)
    memory_mode = out["questions"][0]["modes"]["memory"]
    assert memory_mode["error"] == "" and memory_mode["evidence"]
    # 说明里只带异常**类名**(不把异常文本抄进导出文件),所以按 search.py 自己的标记断言
    from app.memory import search as memory_search
    assert any(memory_search.DEGRADE_VECTOR_DOWN in note for note in memory_mode["degraded"])

    # 2) 两路都不可用:没有可用证据,并明确记 error(不用空结果冒充「没记忆」)
    #    稀疏索引先摘掉(否则关键词走的是索引那一路,退不到进程内 BM25),再让替身返回
    #    search.py 自己那条失败路径的形状(候选为空 + 说明 + 不可用)
    memorykit.sparse_missing(monkeypatch)
    monkeypatch.setattr("app.memory.search._bm25_candidates",
                        lambda *a, **k: ([], "关键词召回不可用(读不到记忆正文),本次只用向量召回",
                                         False))
    out = me.ask(run, dataset, modes=["memory", "no_memory"], answer=False)
    memory_mode = out["questions"][0]["modes"]["memory"]
    assert memory_mode["evidence"] == [] and memory_mode["context_chars"] == 0
    assert memory_mode["error"]
    assert "no_memory" in out["questions"][0]["modes"]      # 其余模式不受牵连


# ---------------------------------------------------------------- 调用统计


def test_usage_stats_counts_memory_purposes_and_keeps_cost_unknown(env, monkeypatch, tmp_path):
    """调用统计按「记忆用途 + 时间窗」取数;缺用量的那条记 unknown,不记 0 元。"""
    from app.config import get_settings as settings_fn
    from app.metering import build_call, install_store, install_writer
    from app.metering.context import CallContext, PURPOSE_MEMORY_SEARCH, PURPOSE_INDEX
    from app.metering.store import MemoryStore
    from app.metering.writer import MeteringWriter

    class _Writer(MeteringWriter):
        def _ensure_thread(self) -> None:      # 不入队后台线程:统计前显式 flush
            return

        def start(self) -> None:
            return

    s = settings_fn()
    monkeypatch.setattr(s, "metering_enabled", True)
    store = MemoryStore()
    install_store(store)
    install_writer(_Writer())
    try:
        run = me.EvalEnv("usage")
        # 窗口内:一条记忆检索(没有用量 → 金额记 unknown),一条索引调用(不算记忆)
        store.insert([
            build_call(service="embedding", provider="dashscope", target="text-embedding-v4",
                       endpoint="https://dashscope.test", duration_ms=10, status="success",
                       usage_quantity=None, ctx=CallContext(purpose=PURPOSE_MEMORY_SEARCH)),
            build_call(service="embedding", provider="dashscope", target="text-embedding-v4",
                       endpoint="https://dashscope.test", duration_ms=10, status="success",
                       usage_quantity=None, ctx=CallContext(purpose=PURPOSE_INDEX)),
        ], [])

        out = me.usage_stats(run)
    finally:
        install_writer(None)
        install_store(None)

    assert "error" not in out
    assert out["by_purpose"]["memory_search"]["calls"] == 1
    assert out["by_purpose"]["memory_extract"]["calls"] == 0        # 别的用途不算进来
    assert out["totals"]["calls"] == 1 and out["totals"]["unknown_cost"] == 1
    assert "独占" in out["note"]                                   # 口径限制写在结果里


# ---------------------------------------------------------------- 清理


def test_purge_clears_only_this_runs_user(env, tmp_path):
    """清理只动本次评测那一份(作用域 × 该作用域内的用户):别的用户一条不动。"""
    from app.memory import vector

    dataset = write_dataset(tmp_path, payload())
    script_extraction(env, FACTS)
    run = me.EvalEnv("purge-me")
    me.build(run, dataset, max_ticks=50)
    memorykit.seed(env, env.db, "别人的记忆", user_id="someone-else")
    assert run_texts(run) and memory_texts("someone-else") == ["别人的记忆"]

    out = me.purge(run)

    assert out["users"] == [run.user_id]                     # 反查出来要清的就是这一个
    assert [c["items"] for c in out["cleared"]] == [3]
    assert out["cleared"][0]["generation"] >= 1
    assert run_texts(run) == []
    assert memory_texts("someone-else") == ["别人的记忆"]            # 别人的一条没动
    assert out["clean"] is True and out["points_left"] == 0          # 索引侧也干净了
    assert vector.count("someone-else") == 1                         # 别人的点还在
    assert vector.collection_exists(run.collection)                  # 集合是正式索引的家,不删
    assert json.loads(run.path("purge.json").read_text(encoding="utf-8"))["clean"] is True


# ---------------------------------------------------------------- 正式与评测互不越界


def test_a_formal_worker_never_claims_eval_jobs_and_vice_versa(env, tmp_path):
    """两类数据在同一套表里共存,靠 scope 分开:各自的 worker 只能认领自己那一批。"""
    from app.memory.worker import MemoryWorker

    dataset = write_dataset(tmp_path, payload(conversations=[
        {"id": "c1", "turns": [{"user": "我习惯用中文写实验记录", "assistant": "明白。"}]}]))
    run = me.EvalEnv("coexist")
    script_extraction(env, FACTS)
    # 评测那一份不驱动 worker,只登记;正式那一份走完整的 build
    service.enqueue_extraction(user_id=run.user_id, scope=run.scope, thread_id="eval:c1",
                                  messages=[{"role": "user", "content": "我习惯用中文写实验记录"},
                                            {"role": "assistant", "content": "明白。"}],
                                  source="eval_build")
    me.build(run, dataset, max_ticks=50)

    # 评测 worker 跑完之后:评测任务进终态,正式队列里一条都没有
    with env.db.session_scope() as session:
        assert repo.job_counts(session, run.user_id, scope=run.scope)["pending"] == 0
        assert repo.job_counts(session, run.user_id, scope=run.scope)["succeeded"] == 1
        assert sum(repo.job_counts(session, scope="").values()) == 0      # 正式队列空着

    # 正式 worker(scope='')在同一个库上跑一轮:既领不到评测任务,也不会给评测数据补索引
    MemoryWorker().run_once(limit=20, force=True)
    with env.db.session_scope() as session:
        assert repo.job_counts(session, run.user_id, scope=run.scope)["succeeded"] == 1
        assert repo.count_pending_index(session, embedding_version=service.embedding_version(),
                                        scope=run.scope) == 0
    assert run_texts(run)                            # 评测记忆还在自己那一份里
    with env.db.session_scope() as session:          # 正式作用域里一条记忆都没有
        assert repo.count_items_in_scope(session, scope="") == 0


def test_formal_reads_never_see_eval_memories(env, tmp_path):
    """检索与列表默认只认正式数据:评测记忆不会出现在「我的记忆」里。"""
    dataset = write_dataset(tmp_path, payload())
    script_extraction(env, FACTS)
    run = me.EvalEnv("invisible")
    me.build(run, dataset, max_ticks=50)
    env.dense.ids = [item.id for item in
                     service.list_items(user_id=run.user_id, limit=100, scope=run.scope)["items"]]

    assert run_texts(run) and len(run_texts(run)) == 3
    # 同一个 user_id、不带 scope(正式路径)时:一条都看不到
    assert memory_texts(run.user_id) == []
    assert service.status(user_id=run.user_id)["items"] == 0
    assert sum(service.status(user_id=run.user_id)["jobs"].values()) == 0
    recall = service.list_items(user_id=run.user_id, query="实验记录")
    assert recall["items"] == []                          # 正式路径的检索也召不回来


def test_two_eval_runs_do_not_interfere(env, tmp_path):
    """两次评测各跑各的:作用域与派生用户都不同,互相召不回、purge 也删不到对方。"""
    dataset = write_dataset(tmp_path, payload(conversations=[
        {"id": "c1", "turns": [{"user": "我习惯用中文写实验记录", "assistant": "明白。"}]}]))
    first, second = me.EvalEnv("run-1"), me.EvalEnv("run-2")
    script_extraction(env, ["用户习惯用中文写实验记录"])
    me.build(first, dataset, max_ticks=50)
    script_extraction(env, ["用户每周五整理实验数据"])
    me.build(second, dataset, max_ticks=50)

    assert first.user_id != second.user_id and first.scope != second.scope
    assert run_texts(first) == ["用户习惯用中文写实验记录"]
    assert run_texts(second) == ["用户每周五整理实验数据"]

    me.purge(first)

    assert run_texts(first) == []
    assert run_texts(second) == ["用户每周五整理实验数据"]    # 另一次评测一条没少


def test_different_subjects_do_not_cross_recall(env, tmp_path):
    """多主体数据集:两个人的记忆分属两个用户 —— A 的问题召回不到 B 的记忆。"""
    dataset = write_dataset(tmp_path, payload(
        conversations=[
            {"id": "c1", "subject": "alice",
             "turns": [{"user": "我习惯用中文写实验记录", "assistant": "明白。"}]},
            {"id": "c2", "subject": "bob",
             "turns": [{"user": "我习惯用英文写实验记录", "assistant": "好的。"}]},
        ],
        questions=[{"id": "q1", "conversation_id": "c1", "question": "我写实验记录用什么语言?"},
                   {"id": "q2", "conversation_id": "c2", "question": "我用什么语言写实验记录?"}]))
    run = me.EvalEnv("subjects")
    script_extraction(env, ["用户习惯用中文写实验记录", "用户习惯用英文写实验记录"])

    summary = me.build(run, dataset, max_ticks=50)

    assert summary["subjects"] == 2 and summary["memories"] == 2
    alice, bob = (me.eval_user_id("subjects", "alice"), me.eval_user_id("subjects", "bob"))
    assert memory_texts(alice, run.scope) == ["用户习惯用中文写实验记录"]
    assert memory_texts(bob, run.scope) == ["用户习惯用英文写实验记录"]

    env.dense.ids = [item.id for item in
                     service.list_items(user_id=alice, limit=100, scope=run.scope)["items"]]
    out = me.ask(run, dataset, modes=["memory"], answer=False)

    rows = {row["id"]: row for row in out["questions"]}
    assert rows["q1"]["memory_user"] == alice and rows["q2"]["memory_user"] == bob
    assert [h["text"] for h in rows["q1"]["modes"]["memory"]["evidence"]] == ["用户习惯用中文写实验记录"]

    # purge 反查到两个主体,一起清掉(谁也没落下)
    assert me.purge(run)["users"] == sorted([alice, bob])
    assert memory_texts(alice, run.scope) == [] and memory_texts(bob, run.scope) == []


# ---------------------------------------------------------------- 清单与 CLI


def test_run_manifest_fixes_models_params_and_never_writes_credentials(env, tmp_path, capsys):
    """`run` 留下可复现清单(代码版本 / 模型 / 参数),且**任何密钥都不进文件**。"""
    from argparse import Namespace

    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(payload(), ensure_ascii=False), encoding="utf-8")
    script_extraction(env, FACTS)

    code = me.cmd_run(Namespace(run_id="cli", dataset=str(dataset_path), modes="no_memory,memory",
                                answer=False, context_chars=me.DEFAULT_CONTEXT_CHARS,
                                keep=True, max_ticks=50, sleep=0.0))
    capsys.readouterr()                                     # 丢掉 stdout:里面不该有密钥

    assert code == 0
    run = me.EvalEnv("cli")
    manifest = json.loads(run.path("run.json").read_text(encoding="utf-8"))
    assert manifest["llm_model"] == env.settings.llm_model
    assert manifest["embeddings_model"] == env.settings.embeddings_model
    assert manifest["memory_maintenance_enabled"] is False   # 固定生成参数:维护关掉
    assert manifest["modes"] == ["no_memory", "memory"]
    assert manifest["dataset"]["digest"]                     # 数据集可核对(哈希)
    assert manifest["code_commit"]                            # 代码版本(仓库里必然拿得到)

    blob = "".join(p.read_text(encoding="utf-8") for p in run.dir.iterdir())
    assert "api_key" not in blob and "secret" not in blob.lower()
    key = env.settings.llm_api_key
    if key:
        assert key not in blob                               # 真 key 值也不许出现在任何产物里


def test_cmd_build_returns_nonzero_when_not_drained(env, tmp_path, monkeypatch, capsys):
    """CLI 退出码如实反映成败:没跑干净返回非 0(便于接进流水线)。"""
    from argparse import Namespace

    class _Stuck:
        def __init__(self, **_kwargs):
            pass

        def run_once(self, **kwargs):
            return {}

    monkeypatch.setattr("app.memory.worker.MemoryWorker", _Stuck)
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(payload(), ensure_ascii=False), encoding="utf-8")
    script_extraction(env, FACTS)

    code = me.cmd_build(Namespace(run_id="cli-stuck", dataset=str(dataset_path), max_ticks=1,
                                  sleep=0.0))
    capsys.readouterr()

    assert code == 1


def test_ask_without_dataset_and_without_history_asks_for_one(env, tmp_path, capsys):
    """单独跑 ask 时既没给数据集、上次也没留档:明确要求给数据集,不用空数据凑合。"""
    from argparse import Namespace

    with pytest.raises(SystemExit):
        me.cmd_ask(Namespace(run_id="no-dataset", dataset="", modes="memory", answer=False,
                             context_chars=100))
    capsys.readouterr()


def test_cmd_build_leaves_the_dataset_for_a_later_ask(env, tmp_path, capsys):
    """build 之后单独跑 ask:不必再指一遍数据集,清单里的哈希仍与构建时对得上。"""
    from argparse import Namespace

    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(payload(), ensure_ascii=False), encoding="utf-8")
    script_extraction(env, FACTS)

    assert me.cmd_build(Namespace(run_id="cli-keep", dataset=str(dataset_path), max_ticks=50,
                                  sleep=0.0)) == 0
    capsys.readouterr()
    code = me.cmd_ask(Namespace(run_id="cli-keep", dataset="", modes="memory", answer=False,
                                context_chars=me.DEFAULT_CONTEXT_CHARS))
    capsys.readouterr()

    run = me.EvalEnv("cli-keep")
    results = json.loads(run.path("results.json").read_text(encoding="utf-8"))
    manifest = json.loads(run.path("run.json").read_text(encoding="utf-8"))
    assert code == 0 and results["questions"]
    assert manifest["dataset"]["digest"]            # 用的就是 build 留下的那份数据集
    assert manifest["dataset"]["path"].endswith("dataset.json")
