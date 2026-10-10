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
