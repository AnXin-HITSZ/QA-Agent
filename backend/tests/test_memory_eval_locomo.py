"""LoCoMo 评测链路(适配 / 严密构建 / 三模式 / 评分 / 报告)的离线自检(§14)。

与 test_memory_eval.py 同一套装配:真记忆表(SQLite)+ 内存 Qdrant + 假 Embeddings /
提取模型 / 聊天模型,检索参数压小。**不涉及任何真实付费调用**,也不构成对真实
MySQL / Qdrant / 模型效果的验证 —— 成绩只能在配齐凭证的独占环境里小样本跑出来。

覆盖的硬规矩(与 memory_eval.py / evalkit 的 docstring 对应):
- 数据适配:会话按数字序号排序(session_2 先于 session_10)、两位说话者都保留身份、
  时间原样保留 + 能解析才标准化、evidence 逐条对账(打包 / 前导零 / 坏形态);
- 构建:一个会话一个任务、带姓名归属说明;队列 / 失败 / 待索引 / 清理台账四项分别记录;
- 提问闸:没有 build.json、数据集摘要不一致、构建不干净 —— 一律拒绝(诊断放行要显式开);
- 评分:失败 / 跳过 / 无参考不记 0;judge 显式开启、失败记 judge_error;micro 与 macro 分开;
- 报告与清理:失败样本进报告;purge 只动本次评测那一份。
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from app.config import get_settings
from app.memory import service
from scripts import memory_eval as me
from scripts.evalkit import locomo, reporting, scoring
from tests import memorykit

FIXTURE = Path(me.__file__).parent / "memory_eval.locomo.sample.json"

# 检索参数压小:小样本下「谁进了最终结果」一眼能看清
PARAMS = {
    "memory_vector_k": 3, "memory_bm25_k": 3, "memory_rerank_k": 3, "memory_top_k": 3,
    "memory_rrf_k": 60, "memory_context_chars": 2000, "memory_bm25_corpus_limit": 1000,
    "memory_rerank_max_docs": 50, "memory_rerank_max_chars": 8000,
}

# 合成样本 4 段会话各配一条提取脚本(内容与数据集的会话一一对应)
FACTS_V2 = ["Alex 搬到了广州,在一家电商公司上班", "Beth 养了一只叫团子的橙猫",
            "Beth 下个月要去成都出差两周", "Dave 今年开始学吉他"]

V1_CONVERSATIONS = [
    {"id": "c1", "turns": [{"user": "我习惯用中文写实验记录", "assistant": "明白。"}]},
]
V1_QUESTIONS = [{"id": "q1", "conversation_id": "c1", "question": "写实验记录用什么语言?",
                 "answer": "中文", "category": 4}]


@pytest.fixture
def env(memory_db, cache_env, monkeypatch, tmp_path):
    """离线评测环境:与 test_memory_eval.py 的 env 同一套装配(运行目录也改到临时目录)。"""
    s = get_settings()
    monkeypatch.setattr(s, "memory_maintenance_enabled", False)   # 固定生成参数:维护关掉
    monkeypatch.setattr(s, "memory_write_enabled", True)
    monkeypatch.setattr(s, "memory_search_enabled", True)
    prod_collection = s.memory_collection
    monkeypatch.setattr(me, "RUNS_DIR", tmp_path / "runs")

    kit = memorykit.install(monkeypatch, settings=s, dim=s.embeddings_dim, params=PARAMS)
    kit.db = memory_db.db
    kit.settings = s
    kit.prod_collection = prod_collection
    kit.memory_llm = memorykit.FakeLLM()
    kit.chat_llm = memorykit.FakeLLM("这是模型的回答。")
    monkeypatch.setattr("app.memory.llm.get_memory_llm", lambda: kit.memory_llm)
    monkeypatch.setattr("app.llm.get_llm", lambda: kit.chat_llm)

    yield kit
    memory_db.db.dispose()


def prepare_file(tmp_path, name="prepared.json", sample_limit=None) -> Path:
    """把随仓库的合成原始数据(官方 locomo10.json 同形)适配成评测数据集文件。"""
    out = tmp_path / name
    locomo.prepare_file(str(FIXTURE), str(out), sample_limit=sample_limit)
    return out


def prepared(tmp_path, name="prepared.json") -> dict:
    return me.load_dataset(str(prepare_file(tmp_path, name)))


def write_v1(tmp_path, *, conversations=None, questions=None, name="dataset-v1.json") -> dict:
    path = tmp_path / name
    path.write_text(json.dumps({"name": "test-sample",
                                "conversations": conversations or V1_CONVERSATIONS,
                                "questions": questions or V1_QUESTIONS},
                               ensure_ascii=False), encoding="utf-8")
    return me.load_dataset(str(path))


def mini_sample() -> dict:
    """最小可用的原始样本(官方结构);各拒绝用例在它上面逐处改坏。"""
    return {
        "sample_id": "s1",
        "conversation": {
            "speaker_a": "A", "speaker_b": "B",
            "session_1": [{"speaker": "A", "dia_id": "D1:1", "text": "我住在杭州。"}],
            "session_1_date_time": "1:00 pm on 1 May, 2024",
        },
        "qa": [{"question": "我住在哪?", "answer": "杭州", "category": 4, "evidence": ["D1:1"]}],
    }


def script_extraction(env, texts: list[str]) -> None:
    env.memory_llm.script = [json.dumps({"facts": [{"text": t, "kind": "preference"}]},
                                        ensure_ascii=False) for t in texts]


def memory_texts(user_id: str, scope: str = "") -> list[str]:
    return [item.text for item in
            service.list_items(user_id=user_id, limit=100, scope=scope)["items"]]


class _StuckWorker:
    """替身:认领了任务却什么都不做(模拟后台一直没跟上)。"""

    def __init__(self, **_kwargs):
        pass

    def run_once(self, **kwargs):
        return {}


# ---------------------------------------------------------------- 数据适配(prepare)


def test_prepare_orders_sessions_numerically(tmp_path):
    """会话按数字序号排序:session_2 必须早于 session_10(不靠字符串字典序)。"""
    dataset = prepared(tmp_path)

    assert [[s["index"] for s in c["sessions"]] for c in dataset["conversations"]] \
        == [[2, 10], [1, 3]]
    assert dataset["schema_version"] == 2


def test_prepare_keeps_speakers_times_and_message_order(tmp_path):
    """两位说话者的身份、会话时间(原样 + 能解析才标准化)、消息顺序都保住。"""
    dataset = prepared(tmp_path)
    conv = dataset["conversations"][0]

    assert conv["speakers"] == {"a": "Alex", "b": "Beth"}
    assert conv["subject"] == "locomo:synth-001"          # 一题一主体:整个样本一个所有者
    session = conv["sessions"][0]
    assert session["date_time_raw"] == "9:15 am on 3 March, 2024"
    assert session["date_time"] == "2024-03-03T09:15:00"  # naive:不擅自赋时区
    assert session["date_time_status"] == "parsed"
    assert [t["dia_id"] for t in session["turns"]] == ["D2:1", "D2:2"]
    assert [t["order"] for t in session["turns"]] == [1, 2]
    assert [t["speaker"] for t in session["turns"]] == ["Alex", "Beth"]


def test_prepare_resolves_evidence_and_flags_bad_forms(tmp_path):
    """evidence 逐条对账:打包拆分、前导零归一、对不上 / 坏形态如实记,原样保留。"""
    dataset = prepared(tmp_path)
    by_id = {q["id"]: q for q in dataset["questions"]}

    packed = by_id["locomo:synth-001:q6"]
    assert packed["evidence"] == ["D2:1", "D10:2"]           # 'D02:01; D10:2' 拆开并去前导零
    assert packed["evidence_raw"] == ["D02:01; D10:2"]       # 原始形态照留
    partial = by_id["locomo:synth-001:q7"]
    assert partial["evidence_status"] == "partial"
    assert partial["evidence_unresolved"] == ["D9:9"]        # 对不上的引用单独列出
    bad = by_id["locomo:synth-001:q8"]
    assert bad["evidence_status"] == "unresolved" and bad["evidence_unresolved"] == ["D"]
    none = by_id["locomo:synth-001:q5"]
    assert none["evidence_status"] == "none" and none["evidence"] == []


def test_prepare_keeps_identity_in_history_and_extraction(tmp_path):
    """历史文本与提取输入都保留姓名、时间与图片描述;两人都不被映射成 assistant。"""
    dataset = prepared(tmp_path)
    conv = dataset["conversations"][0]

    lines = locomo.history_lines(conv)
    assert lines[0] == "[会话 2｜9:15 am on 3 March, 2024]"
    assert "分享了图片,图片描述:一只橙色猫咪趴在窗台上" in "\n".join(lines)

    messages = locomo.session_messages(conv["sessions"][0])
    assert all(m["role"] == "user" for m in messages)        # 两人的话都在,身份不丢
    assert messages[0]["content"].startswith("[会话记录时间:")
    assert messages[1]["content"].startswith("Alex: ")
    assert messages[2]["content"].startswith("Beth: ")


def test_prepare_notes_orphan_and_unparsable_dates(tmp_path):
    """合法但可疑的形态不猜、不丢:孤立日期键与无法解析的时间都记进来源备注。"""
    dataset = prepared(tmp_path)
    notes = "\n".join(dataset["source"]["notes"])

    assert "孤立日期键忽略 1 个" in notes                    # synth-001 的 session_7_date_time
    assert "无法解析 1 段" in notes                          # synth-002 的 'unknown time'


def test_prepare_rejects_bad_category():
    sample = mini_sample()
    sample["qa"][0]["category"] = 9
    with pytest.raises(locomo.LocomoDataError, match="类别"):
        locomo.prepare([sample])


def test_prepare_rejects_unknown_speaker():
    sample = mini_sample()
    sample["conversation"]["session_1"][0]["speaker"] = "C"
    with pytest.raises(locomo.LocomoDataError, match="说话者"):
        locomo.prepare([sample])


@pytest.mark.parametrize("answer", [2022, 2, 0, 3.5])
def test_prepare_preserves_numeric_answers(answer):
    sample = mini_sample()
    sample["qa"][0]["answer"] = answer
    dataset = locomo.prepare([sample])
    assert dataset["questions"][0]["answer"] == str(answer)


@pytest.mark.parametrize("answer", [True, False, {}, [], float("nan"), float("inf")])
def test_prepare_rejects_invalid_answer_types(answer):
    sample = mini_sample()
    sample["qa"][0]["answer"] = answer
    with pytest.raises(locomo.LocomoDataError, match="answer"):
        locomo.prepare([sample])


def test_prepare_rejects_missing_answer_for_answerable_question():
    sample = mini_sample()
    sample["qa"][0].pop("answer")
    with pytest.raises(locomo.LocomoDataError, match="answer"):
        locomo.prepare([sample])


def test_prepare_rejects_duplicate_sample_ids():
    with pytest.raises(locomo.LocomoDataError, match="重复"):
        locomo.prepare([mini_sample(), mini_sample()])


def test_prepare_sample_limit_takes_first_n(tmp_path):
    dataset = locomo.prepare(json.loads(FIXTURE.read_text(encoding="utf-8")), sample_limit=1)

    assert [c["sample_id"] for c in dataset["conversations"]] == ["synth-001"]
    assert dataset["questions"] and all(q["sample_id"] == "synth-001"
                                        for q in dataset["questions"])


# ---------------------------------------------------------------- 数据集加载与摊平


def test_v2_digest_is_path_independent_and_excludes_metadata(tmp_path):
    """摘要只认内容:同一份数据换个路径哈希不变;`_` 前缀的元数据不参与。"""
    first = prepared(tmp_path, "a.json")
    second = prepared(tmp_path, "b.json")

    assert first["_digest"] == second["_digest"]
    assert me.dataset_digest(first) == first["_digest"]


def test_v2_loader_rejects_unknown_conversation_and_schema(tmp_path):
    """未知引用与不认识的 schema 版本都拒绝,不带半份数据往下跑。"""
    path = prepare_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["questions"][0]["conversation_id"] = "locomo:missing"
    bad_ref = tmp_path / "bad-ref.json"
    bad_ref.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit, match="conversation_id"):
        me.load_dataset(str(bad_ref))

    bad_schema = tmp_path / "bad-schema.json"
    bad_schema.write_text(json.dumps({"schema_version": 3, "conversations": [],
                                      "questions": []}), encoding="utf-8")
    with pytest.raises(SystemExit, match="schema_version"):
        me.load_dataset(str(bad_schema))


def test_v2_exchanges_are_one_unit_per_session_in_order(tmp_path):
    """一个会话一个提取单元、按数字序号排列;单元带主体、稳定的幂等键与适配说明。"""
    dataset = prepared(tmp_path)

    units = me.exchanges(dataset)

    assert [u["thread_id"] for u in units] == [
        "eval:locomo:synth-001:s2", "eval:locomo:synth-001:s10",
        "eval:locomo:synth-002:s1", "eval:locomo:synth-002:s3"]
    assert [u["subject"] for u in units] == ["locomo:synth-001"] * 2 + ["locomo:synth-002"] * 2
    for unit in units:
        assert all(m["role"] == "user" for m in unit["messages"])
        assert unit["extract_note"] == locomo.EXTRACT_NOTE
        assert unit["dedupe_key"] == unit["thread_id"]      # 同会话重复 build 不重复付费


# ---------------------------------------------------------------- 严密构建


def test_build_over_locomo_dataset_is_ordered_and_attributed(env, tmp_path):
    """v2 构建:每会话一个任务、逐会话顺序执行;姓名归属说明进提取提示;重复 build 不重跑。"""
    dataset = prepared(tmp_path)
    script_extraction(env, FACTS_V2)
    run = me.EvalEnv("locomo-build")

    summary = me.build(run, dataset, max_ticks=50)

    assert summary["exchanges"] == 4 and summary["jobs_queued"] == 4
    assert summary["exchanges_processed"] == 4
    assert summary["drained"] is True and summary["complete"] is True
    assert summary["memories"] == 4
    assert summary["extract_note"] == {"used": True, "version": locomo.EXTRACT_NOTE_VERSION}
    assert summary["dataset"]["schema_version"] == 2
    assert summary["cleanup"] == {"pending": 0, "failed": 0}

    # 会话按序处理:session_2 的提取先于 session_10,后面的会话不提前喂进去
    prompts = env.memory_llm.prompts
    assert len(prompts) == 4
    assert "两个人的对话" in prompts[0]                      # 适配说明真的进了提示
    assert "[会话记录时间:" in prompts[0]
    assert "Alex: 我上周搬到了广州" in prompts[0]
    assert "陶艺班" not in prompts[0] and "陶艺班" in prompts[1]

    # 两个样本的记忆分属两个用户(隔离由主体决定)
    first = me.eval_user_id("locomo-build", "locomo:synth-001")
    second = me.eval_user_id("locomo-build", "locomo:synth-002")
    assert len(memory_texts(first, run.scope)) == 2 and len(memory_texts(second, run.scope)) == 2

    # 重复 build:同一会话的幂等键拦下重复入队(不再产生付费调用)
    calls_before = len(env.memory_llm.calls)
    again = me.build(run, dataset, max_ticks=50)
    assert again["jobs_queued"] == 0 and again["memories"] == 4
    assert len(env.memory_llm.calls) == calls_before


def test_build_failed_jobs_make_build_incomplete_and_ask_refuses(env, tmp_path):
    """有失败任务就不算 complete:即使队列收尾了也拒绝进入正常评测。"""
    dataset = write_v1(tmp_path)
    env.memory_llm.script = ["这不是 JSON 输出"]            # 提取始终不合规 → 永久失败
    run = me.EvalEnv("build-failed")

    summary = me.build(run, dataset, max_ticks=20)

    assert summary["drained"] is True                        # 失败是终态,队列确实收尾了
    assert summary["jobs_failed"] == 1
    assert summary["complete"] is False
    with pytest.raises(SystemExit, match="构建未完成"):
        me.ask(run, dataset, modes=["no_memory"], answer=False)


# ---------------------------------------------------------------- 提问闸


def test_ask_requires_build_and_matching_dataset(env, tmp_path):
    """没有 build.json 拒绝;数据集与构建时不一致也拒绝(换了数据集不能复用旧记忆)。"""
    dataset = prepared(tmp_path)
    run = me.EvalEnv("gate")
    with pytest.raises(SystemExit, match="build"):
        me.ask(run, dataset, modes=["no_memory"], answer=False)

    script_extraction(env, FACTS_V2)
    me.build(run, dataset, max_ticks=50)

    other = write_v1(tmp_path)                               # 另一个数据集:摘要不同
    with pytest.raises(SystemExit, match="不一致"):
        me.ask(run, other, modes=["no_memory"], answer=False)


def test_ask_strictly_rejects_unknown_conversation_id(env, tmp_path):
    """问题的 conversation_id 必须命中:未知引用直接报错,不回退到全部历史。"""
    dataset = write_v1(tmp_path, questions=[
        {"id": "q9", "conversation_id": "nope", "question": "随便问问?", "answer": "答",
         "category": 4}])
    script_extraction(env, ["用户习惯用中文写实验记录"])
    run = me.EvalEnv("strict")
    me.build(run, dataset, max_ticks=50)

    with pytest.raises(SystemExit, match="conversation_id"):
        me.ask(run, dataset, modes=["no_memory"], answer=False)


def test_ask_incomplete_build_refused_unless_explicitly_allowed(env, tmp_path, monkeypatch):
    """构建没收尾:默认拒绝;显式放行(--allow-incomplete)后结果标记无效。"""
    monkeypatch.setattr("app.memory.worker.MemoryWorker", _StuckWorker)
    dataset = write_v1(tmp_path)
    run = me.EvalEnv("gate-incomplete")
    summary = me.build(run, dataset, max_ticks=1)
    assert summary["complete"] is False

    with pytest.raises(SystemExit, match="构建未完成"):
        me.ask(run, dataset, modes=["no_memory"], answer=False)

    out = me.ask(run, dataset, modes=["no_memory"], answer=False, allow_incomplete=True)
    assert out["run_valid"] is False and "诊断性放行" in out["invalid_reason"]


def test_ask_routes_each_question_to_its_subject(env, tmp_path):
    """v2 对话按主体落到各自的评测用户;证据与标准 evidence 都导出(供人工诊断)。"""
    dataset = prepared(tmp_path)
    script_extraction(env, FACTS_V2)
    run = me.EvalEnv("locomo-ask")
    me.build(run, dataset, max_ticks=50)
    first = me.eval_user_id("locomo-ask", "locomo:synth-001")
    env.dense.ids = [item.id for item in
                     service.list_items(user_id=first, limit=100, scope=run.scope)["items"]]

    out = me.ask(run, dataset, modes=["no_memory", "memory", "full_context"], answer=False)

    rows = {row["id"]: row for row in out["questions"]}
    assert len(rows) == 10
    row = rows["locomo:synth-001:q1"]
    assert row["memory_user"] == first
    assert row["category"] == 4 and row["category_name"] == "single-hop"
    assert row["evidence_expected"] == ["D2:2"]              # 标准 evidence 随行导出
    assert row["modes"]["memory"]["evidence"]                # 召回证据带 memory_id 等明细
    full = row["modes"]["full_context"]
    assert full["history_chars"] > 0 and full["over_window"] is False
    assert rows["locomo:synth-002:q1"]["memory_user"] == me.eval_user_id(
        "locomo-ask", "locomo:synth-002")


# ---------------------------------------------------------------- 评分(纯本地)


def test_scoring_fixed_inputs_follow_official_shape():
    """F1 / BLEU-1 的固定输入口径:去冠词、两侧都空记 0、类别 1 多答案按逗号取最大。"""
    assert scoring.f1_score("中文", "中文") == 1.0
    assert scoring.f1_score("", "") == 0.0                   # 官方口径:交集为空记 0(含两侧都空)
    assert scoring.f1_score("the cat", "cat") == 1.0
    assert 0 < scoring.f1_score("猫 狗", "猫") < 1
    # 类别 1 的多答案:对每个参考答案取各预测片段的最大值再平均
    # (注意别用单字母做输入:官方 normalize 会把独立的 a/an/the/and 去掉)
    assert scoring.f1_multi("cat, dog", "cat, bird") == pytest.approx(0.5)
    assert scoring.bleu1("中文 测试", "中文 测试") == 1.0
    assert scoring.bleu1("", "有参考") == 0.0


def test_scoring_special_categories_and_empty_reference():
    """类别 5 按官方「明确说没有信息」判、类别 3 取分号前第一段;无参考不评分不记 0。"""
    cat5 = scoring.score_answer(5, "I think there is no information available about that", "")
    assert cat5["f1"] == 1.0 and cat5["f1_rule"] == "locomo-cat5-refusal"
    assert cat5["bleu1"] is None and cat5["bleu1_rule"] == "adversarial-no-reference"
    assert scoring.score_answer(5, "她养了一只猫", "Beth 的猫叫团子")["f1"] == 0.0

    cat3 = scoring.score_answer(3, "杭州", "杭州; 浙江省杭州市")
    assert cat3["f1"] == 1.0 and cat3["f1_rule"] == "locomo-cat3-first-segment"

    empty_ref = scoring.score_answer(4, "随便写", "")
    assert empty_ref["f1"] is None and "不记 0" in empty_ref["note"]


def test_aggregate_micro_vs_macro_and_excludes_failures():
    """micro 是逐题平均、macro 是类别宏平均;失败剔除出分母并单独计数。"""
    rows = [
        {"id": "q1", "category": 4, "category_name": "single-hop",
         "modes": {"memory": {"status": "scored", "f1": 1.0, "bleu1": 1.0}}},
        {"id": "q2", "category": 4, "category_name": "single-hop",
         "modes": {"memory": {"status": "failed", "f1": None, "bleu1": None}}},
        {"id": "q3", "category": 1, "category_name": "multi-hop",
         "modes": {"memory": {"status": "scored", "f1": 0.0, "bleu1": 0.0}}},
        {"id": "q4", "category": 1, "category_name": "multi-hop",
         "modes": {"memory": {"status": "scored", "f1": 0.0, "bleu1": 0.0}}},
    ]
    out = scoring.aggregate(rows, ["memory"])["by_mode"]["memory"]

    assert out["mean_f1"] == pytest.approx(1 / 3)           # 逐题:(1+0+0)/3
    assert out["macro_f1"] == pytest.approx(0.5)             # 类别:1 和 0 的未加权平均
    assert out["counts"] == {"scored": 3, "failed": 1, "skipped": 0, "judged": 0,
                             "judge_errors": 0, "judge_skipped": 0,
                             "scored_f1": 3, "scored_bleu1": 3}
    assert out["failed_question_ids"] == ["q2"]
    assert "不可混用" in scoring.aggregate(rows, ["memory"])["note"]


def test_score_results_failure_is_not_a_zero():
    """回答调用失败记 failed、不评分不记 0;未请求 judge 时协议里没有裁判。"""
    results = {"run_id": "r", "answer_calls": True, "questions": [
        {"id": "q1", "conversation_id": "c1", "category": 4, "reference": "中文",
         "question": "用什么语言?",
         "modes": {"no_memory": {"answer": "", "answer_error": "RuntimeError: boom",
                                 "error": ""},
                   "memory": {"answer": "中文", "answer_error": "", "error": ""}}}]}

    scores = scoring.score_results(results, metrics=["f1", "bleu1"])

    modes = scores["per_question"][0]["modes"]
    assert modes["no_memory"]["status"] == "failed" and modes["no_memory"]["f1"] is None
    assert modes["memory"]["status"] == "scored" and modes["memory"]["f1"] == 1.0
    no_memory = scores["summary"]["by_mode"]["no_memory"]
    assert no_memory["counts"]["failed"] == 1 and no_memory["mean_f1"] is None
    assert scores["protocol"]["judge"] is None              # 没请求 judge:零裁判调用
    assert scores["protocol"]["bleu1"]["name"] == "qa-agent-bleu1"


def test_judge_retries_bad_format_and_error_is_never_a_zero():
    """裁判输出格式错有限重试;始终不合规记 judge_error,不计入准确率分母、不记 0。"""
    good = json.dumps({"correct": True, "reason": "回答与参考答案一致"}, ensure_ascii=False)
    verdict = scoring.judge_one("问题", "参考", "参考", llm=memorykit.FakeLLM("这不是 JSON", good),
                                retries=1)
    assert verdict["correct"] is True and verdict["attempts"] == 2 and verdict["error"] == ""

    results = {"run_id": "r", "answer_calls": True, "questions": [
        {"id": "q1", "conversation_id": "c1", "category": 4, "reference": "中文",
         "question": "用什么语言?",
         "modes": {"memory": {"answer": "中文", "answer_error": "", "error": ""}}}]}
    scores = scoring.score_results(results, metrics=["f1", "judge"],
                                   judge_llm=memorykit.FakeLLM("坏输出"), judge_retries=0)
    record = scores["per_question"][0]["modes"]["memory"]
    assert record["judge_error"] and record["judge"] is None
    summary = scores["summary"]["by_mode"]["memory"]
    assert summary["counts"]["judge_errors"] == 1 and summary["judge_accuracy"] is None
    assert scores["protocol"]["judge"]["version"] == scoring.JUDGE_PROTOCOL_VERSION
    assert scores["protocol"]["judge"]["model"] == ""       # 未传模型名:如实记空


def test_judge_prompt_never_exposes_mode_names():
    """裁判只看问题 / 参考 / 回答:提示词里不出现任何模式名。"""
    prompt = scoring.judge_prompt("问题", "参考", "回答", unanswerable=False)

    for mode in ("no_memory", "memory", "full_context"):
        assert mode not in prompt


# ---------------------------------------------------------------- 用量与耗时统计


def test_llm_usage_counts_unknown_without_faking_zeros():
    """回答调用 token:缺用量记 unknown;全缺时总量是 None(不记 0)。"""
    questions = [
        {"modes": {"memory": {"answer_usage": {"input_tokens": 10, "output_tokens": 5,
                                               "total_tokens": 15}}}},
        {"modes": {"memory": {"answer_usage": None}}},
        {"modes": {"no_memory": {"answer_usage": None}}},
    ]
    out = me._llm_usage(questions, answer_calls=True)

    assert (out["attempted"], out["known"], out["unknown"]) == (3, 1, 2)
    assert out["total_tokens"] == 15 and out["input_tokens"] == 10
    assert out["by_mode"]["memory"] == {"attempted": 2, "known": 1, "unknown": 1}

    empty = me._llm_usage([{"modes": {"memory": {"answer_usage": None}}}], answer_calls=True)
    assert empty["total_tokens"] is None and empty["unknown"] == 1


def test_latency_stats_nearest_rank():
    assert reporting.latency_stats([10, 20, 30, 40, 50]) == {
        "n": 5, "method": "nearest-rank", "p50": 30, "p95": 50, "max": 50}
    assert reporting.latency_stats([]) is None


# ---------------------------------------------------------------- 报告


def test_report_lists_failures_and_marks_invalid_runs(env, tmp_path, capsys):
    """回答失败如实进报告(不记 0);评分、报告全在本地跑,不动检索也不发模型调用。"""
    dataset = write_v1(tmp_path)
    script_extraction(env, ["用户习惯用中文写实验记录"])
    run = me.EvalEnv("report-me")
    me.build(run, dataset, max_ticks=50)
    env.chat_llm.error = RuntimeError("回答服务不可用")
    out = me.ask(run, dataset, modes=["no_memory", "memory"], answer=True)
    env.chat_llm.error = None
    assert all(row["modes"][mode]["answer_error"] for row in out["questions"]
               for mode in row["modes"])                     # 两模式都失败,如实记录

    dense_before, chat_before = len(env.dense.calls), len(env.chat_llm.calls)
    assert me.cmd_score(Namespace(run_id="report-me", metrics="f1,bleu1", judge_retries=1)) == 0
    assert me.cmd_report(Namespace(run_id="report-me")) == 0
    capsys.readouterr()

    assert len(env.dense.calls) == dense_before and len(env.chat_llm.calls) == chat_before
    report = json.loads(run.path("report.json").read_text(encoding="utf-8"))
    assert report["failures"] and all(f["kind"] == "回答调用/模式执行失败"
                                      for f in report["failures"])
    summary = report["summary"]["by_mode"]
    assert summary["no_memory"]["counts"]["failed"] == 1
    assert summary["no_memory"]["mean_f1"] is None          # 失败不记 0:没有可平均的样本
    markdown = run.path("report.md").read_text(encoding="utf-8")
    assert "三模式对照" in markdown and "失败样本" in markdown and "口径与局限" in markdown


# ---------------------------------------------------------------- 清理


def test_purge_over_locomo_run_clears_all_subjects_only(env, tmp_path):
    """purge 反查到两个主体的用户,一起清;别人的数据与集合一个不动。"""
    from app.memory import vector

    dataset = prepared(tmp_path)
    script_extraction(env, FACTS_V2)
    run = me.EvalEnv("locomo-purge")
    me.build(run, dataset, max_ticks=50)
    memorykit.seed(env, env.db, "别人的记忆", user_id="someone-else")

    out = me.purge(run)

    assert set(out["users"]) == {me.eval_user_id("locomo-purge", "locomo:synth-001"),
                                 me.eval_user_id("locomo-purge", "locomo:synth-002")}
    assert out["clean"] is True and out["status"] == "clean"
    assert out["points_left"] == 0 and out["cleanup"] == {"pending": 0, "failed": 0}
    assert memory_texts("someone-else") == ["别人的记忆"]
    assert vector.count("someone-else") == 1
    assert vector.collection_exists(run.collection)          # 集合是正式索引的家,不删


# ---------------------------------------------------------------- CLI 全链路(全离线)


def test_cmd_prepare_writes_dataset_without_any_service(tmp_path, capsys):
    """prepare 是纯文件操作:不连库、不调模型,产出可直接被 load_dataset 吃下。"""
    out_path = tmp_path / "prepared.json"

    code = me.cmd_prepare(Namespace(input=str(FIXTURE), output=str(out_path), sample_limit=1))

    printed = json.loads(capsys.readouterr().out)
    assert code == 0 and out_path.exists()
    assert printed["samples"] == 1 and printed["questions"] == 8
    dataset = me.load_dataset(str(out_path))
    assert dataset["schema_version"] == 2 and dataset["source"]["kind"] == "locomo"


def test_cli_chain_build_ask_score_report(env, tmp_path, capsys):
    """build → ask(不生成答案)→ score → report 全链路离线跑通,产物齐全、无密钥。"""
    prepared_path = prepare_file(tmp_path)
    script_extraction(env, FACTS_V2)

    assert me.cmd_build(Namespace(run_id="chain", dataset=str(prepared_path), max_ticks=50,
                                  sleep=0.0)) == 0
    assert me.cmd_ask(Namespace(run_id="chain", dataset="",
                                modes="no_memory,memory,full_context", answer=False,
                                context_chars=me.DEFAULT_CONTEXT_CHARS)) == 0
    assert me.cmd_score(Namespace(run_id="chain", metrics="f1,bleu1", judge_retries=1)) == 0
    assert me.cmd_report(Namespace(run_id="chain")) == 0
    capsys.readouterr()

    run = me.EvalEnv("chain")
    for name in ("build.json", "dataset.json", "results.json", "run.json",
                 "scores.json", "report.json", "report.md"):
        assert run.path(name).exists(), name
    scores = json.loads(run.path("scores.json").read_text(encoding="utf-8"))
    assert scores["summary"]["by_mode"]["memory"]["counts"]["skipped"] == 10   # 未生成答案
    manifest = json.loads(run.path("run.json").read_text(encoding="utf-8"))
    assert manifest["build"]["complete"] is True
    assert manifest["answer_instruction_version"] == me.INSTRUCTION_VERSION
    assert "参考信息" in manifest["answer_instruction"]     # 统一回答指令随清单留档

    blob = "".join(p.read_text(encoding="utf-8") for p in run.dir.iterdir() if p.is_file())
    assert "api_key" not in blob and "secret" not in blob.lower()


def test_cli_ask_allow_incomplete_returns_nonzero(env, tmp_path, monkeypatch, capsys):
    """诊断性放行走显式开关,且退出码非 0;不放行时直接拒绝。"""
    monkeypatch.setattr("app.memory.worker.MemoryWorker", _StuckWorker)
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps({"name": "t", "conversations": V1_CONVERSATIONS,
                                        "questions": V1_QUESTIONS}, ensure_ascii=False),
                            encoding="utf-8")
    assert me.cmd_build(Namespace(run_id="diag", dataset=str(dataset_path), max_ticks=1,
                                  sleep=0.0)) == 1
    capsys.readouterr()

    with pytest.raises(SystemExit, match="构建未完成"):
        me.cmd_ask(Namespace(run_id="diag", dataset="", modes="no_memory", answer=False,
                             context_chars=100))
    capsys.readouterr()

    code = me.cmd_ask(Namespace(run_id="diag", dataset="", modes="no_memory", answer=False,
                                context_chars=100, allow_incomplete=True))
    capsys.readouterr()
    assert code == 1


def test_cli_run_skips_ask_when_build_incomplete(env, tmp_path, monkeypatch, capsys):
    """run 必须先构建成功才提问:没通过就不产出 results.json,退出码非 0。"""
    monkeypatch.setattr("app.memory.worker.MemoryWorker", _StuckWorker)
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps({"name": "t", "conversations": V1_CONVERSATIONS,
                                        "questions": V1_QUESTIONS}, ensure_ascii=False),
                            encoding="utf-8")

    code = me.cmd_run(Namespace(run_id="run-stuck", dataset=str(dataset_path),
                                modes="no_memory", answer=False, context_chars=100, keep=True,
                                max_ticks=1, sleep=0.0))
    capsys.readouterr()

    run = me.EvalEnv("run-stuck")
    assert code == 1
    assert not run.path("results.json").exists()             # 拒绝提问:没有半份结果
    manifest = json.loads(run.path("run.json").read_text(encoding="utf-8"))
    assert manifest["build"]["complete"] is False


def test_extract_note_participates_in_reuse_digest():
    """适配说明参与提取阶段的复用摘要:同一段对话、不同说明不被当成同一份输入。"""
    from app.memory.worker import _messages_digest

    messages = [{"role": "user", "content": "Alex: 你好"}]
    assert _messages_digest(messages) == _messages_digest(messages)
    assert _messages_digest(messages) != _messages_digest(messages, locomo.EXTRACT_NOTE)
