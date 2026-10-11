"""Named experiments share build artifacts but keep their output separate."""
import json
import pytest
from scripts import memory_eval as me
from scripts.evalkit.reporting import build_report


def test_variant_paths_share_only_build(monkeypatch, tmp_path):
    monkeypatch.setattr(me, "RUNS_DIR", tmp_path)
    a, b = me.EvalEnv("build-one", variant="nograph"), me.EvalEnv("build-one", variant="graph")
    assert a.path("build.json") == b.path("build.json")
    assert a.path("results.json") != b.path("results.json")


def test_variant_report_reads_shared_build(tmp_path):
    root = tmp_path / "run"
    variant = root / "variants" / "graph"
    variant.mkdir(parents=True)
    (root / "build.json").write_text(json.dumps({"complete": True, "memories": 7}))
    (root / "dataset.json").write_text(json.dumps({"name": "shared", "questions": []}))
    (variant / "results.json").write_text(json.dumps({"run_id": "run", "run_valid": True, "questions": []}))
    report = build_report(variant)
    assert report["build"]["memories"] == 7


def test_compare_rejects_scores_from_different_answers(tmp_path):
    from scripts.compare_memory_variants import compare
    path = tmp_path / "variants" / "graph"
    path.mkdir(parents=True)
    for name, data in (("run", {}), ("results", {"run_valid": True, "questions": []}),
                       ("scores", {"results_digest": "wrong"})):
        (path / (name + ".json")).write_text(json.dumps(data))
    with pytest.raises(ValueError, match="评分不属于当前答案"):
        compare(tmp_path, ["graph"])


def test_runtime_faults_are_not_confused_with_budget_diagnostics():
    from scripts.compare_memory_variants import runtime_issues
    result = {"questions": [{"id": "q", "modes": {"memory": {
        "degraded": ["图遍历达到预算上限，路径覆盖已受限"]}}}]}
    assert runtime_issues(result) == []
    result["questions"][0]["modes"]["memory"]["degraded"].append("记忆向量召回不可用")
    assert len(runtime_issues(result)) == 1


def test_comparison_preserves_previous_reports_and_flags_service_fault(tmp_path):
    from scripts.compare_memory_variants import compare
    from scripts.evalkit.scoring import results_digest
    legacy = tmp_path / "variant-comparison.md"
    legacy.write_text("previous report")
    (tmp_path / "dataset.json").write_text(json.dumps({"questions": [{"id": "q"}]}))
    for name in ("off", "on"):
        path = tmp_path / "variants" / name
        path.mkdir(parents=True)
        results = {"run_valid": True, "questions": [{"id": "q", "question": "test",
            "modes": {"memory": {"degraded": []}}}]}
        manifest = {"source_digest": "same", "modes": ["memory"],
                    "graph": {"switches": {"search": name == "on"}}}
        scores = {"results_digest": results_digest(results), "protocol": {},
                  "summary": {"by_mode": {"memory": {"mean_f1": 0.5, "counts": {}}}}}
        for filename, value in (("run", manifest), ("results", results), ("scores", scores)):
            (path / (filename + ".json")).write_text(json.dumps(value))
    first = compare(tmp_path, ["off", "on"])
    assert first["comparable"]
    path = tmp_path / "variants" / "off"
    results["questions"][0]["modes"]["memory"]["degraded"] = ["记忆向量召回不可用"]
    scores["results_digest"] = results_digest(results)
    (path / "results.json").write_text(json.dumps(results))
    (path / "scores.json").write_text(json.dumps(scores))
    second = compare(tmp_path, ["off", "on"])
    assert not second["comparable"] and second["configurations_match"]
    assert first["report_directory"] != second["report_directory"]
    assert legacy.read_text() == "previous report"
