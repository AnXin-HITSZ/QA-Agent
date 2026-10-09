"""评测有效性、结果绑定与清理判据的离线回归；不调用真实服务。"""
import json
from argparse import Namespace

import pytest
from scripts import memory_eval as me
from scripts.evalkit import scoring, reporting
from tests.test_memory_eval import env, payload  # noqa: F401


def test_full_context_budget_skips_without_call(monkeypatch):
    monkeypatch.setattr(me, "_answer", lambda *a: pytest.fail("超预算不能调用模型"))
    item = me._mode_full_context(payload()["conversations"][0], {"question": "q"}, True, 1)
    assert item["skipped"] and item["over_window"] and item["answer_usage"] is None
    results = {"answer_calls": True, "questions": [{"id": "q", "reference": "x",
               "modes": {"full_context": item}}]}
    scores = scoring.score_results(results, metrics=["f1", "bleu1"])
    assert scores["summary"]["by_mode"]["full_context"]["counts"]["skipped"] == 1
    assert me._llm_usage(results["questions"], answer_calls=True)["attempted"] == 0


def test_missing_required_answer_not_full_bleu():
    assert scoring.score_answer(1, "Paris", "Paris, London")["bleu1"] < 1
    assert scoring.score_answer(1, "Paris London", "Paris, London")["bleu1"] == 1


def test_invalid_build_rejected_before_judge():
    with pytest.raises(ValueError, match="无效构建"):
        scoring.score_results({"run_valid": False}, metrics=["judge"], judge_llm=object())


def test_stale_scores_rejected(tmp_path):
    results = {"run_valid": True, "answer_calls": True, "questions": []}
    scores = scoring.score_results(results, metrics=["f1"])
    (tmp_path / "scores.json").write_text(json.dumps(scores), encoding="utf-8")
    results["questions"].append({"id": "new"})
    (tmp_path / "results.json").write_text(json.dumps(results), encoding="utf-8")
    with pytest.raises(ValueError, match="不匹配"):
        reporting.build_report(tmp_path)


def test_judge_bool_is_strict():
    with pytest.raises(scoring.JudgeFormatError):
        scoring._parse_verdict('{"correct": "true", "reason": "ok"}')


def test_changed_input_rejected_before_enqueue(env, tmp_path, monkeypatch):
    monkeypatch.setattr(me, "exchanges", lambda _: [])
    run = me.EvalEnv("immutable")
    dataset = payload()
    dataset["_digest"] = me.dataset_digest(dataset)
    me.build(run, dataset)
    old = run.path("dataset.json").read_text(encoding="utf-8")
    changed = {**dataset, "name": "changed"}
    with pytest.raises(SystemExit, match="新的 run-id"):
        me.build(run, changed)
    assert run.path("dataset.json").read_text(encoding="utf-8") == old


def test_purge_pending_is_not_clean(env, monkeypatch):
    monkeypatch.setattr(me, "users_in_scope", lambda _: ["u"])
    monkeypatch.setattr(me.service, "clear_user", lambda **k: {})
    monkeypatch.setattr(me.service, "status", lambda **k: {"cleanup_pending": 1})
    monkeypatch.setattr("app.memory.worker.MemoryWorker.run_once", lambda *a, **k: None)
    monkeypatch.setattr(me.vector, "count", lambda **k: 0)
    result = me.purge(me.EvalEnv("pending"))
    assert result["clean"] is False and result["cleanup"]["pending"] == 1


def test_judge_requires_explicit_configuration(env):
    run = me.EvalEnv("judge-choice")
    run.write("results.json", {"run_valid": True, "answer_calls": True, "questions": []})
    with pytest.raises(SystemExit, match="裁判须独立指定"):
        me.cmd_score(Namespace(run_id=run.run_id, metrics="judge", judge_retries=0))


def test_prepare_timestamp_does_not_change_logical_digest():
    first = {**payload(), "source": {"prepared_at": "t1", "input_path": "a", "version": 1}}
    second = {**payload(), "source": {"prepared_at": "t2", "input_path": "b", "version": 1}}
    assert me.dataset_digest(first) == me.dataset_digest(second)


def test_build_config_change_rejected(env, monkeypatch):
    monkeypatch.setattr(me, "exchanges", lambda _: [])
    run = me.EvalEnv("config-immutable")
    dataset = payload()
    me.build(run, dataset)
    monkeypatch.setattr(env.settings, "llm_model", "other-model")
    with pytest.raises(SystemExit, match="新的 run-id"):
        me.build(run, dataset)


def test_ask_invalidates_previous_scores_and_report(env, monkeypatch):
    monkeypatch.setattr(me, "exchanges", lambda _: [])
    run = me.EvalEnv("ask-again")
    dataset = payload()
    dataset["_digest"] = me.dataset_digest(dataset)
    me.build(run, dataset)
    for name in ("scores.json", "report.json", "report.md"):
        run.path(name).write_text("old", encoding="utf-8")
    me.ask(run, dataset, modes=["no_memory"], answer=False)
    assert all(not run.path(name).exists() for name in ("scores.json", "report.json", "report.md"))


def test_purged_run_cannot_be_asked(env):
    run = me.EvalEnv("purged")
    run.write("purge.json", {"clean": True})
    with pytest.raises(SystemExit, match="清理"):
        me._build_state(run, payload())


def test_prepare_question_limit(tmp_path):
    run_output = tmp_path / "prepared.json"
    source = me.SCRIPTS / "memory_eval.locomo.sample.json"
    assert me.main(["prepare", "--input", str(source), "--output", str(run_output),
                    "--sample-limit", "1", "--question-limit", "1"]) == 0
    dataset = json.loads(run_output.read_text(encoding="utf-8"))
    assert len(dataset["questions"]) == 1
    assert len(dataset["conversations"][0]["sessions"]) > 0
