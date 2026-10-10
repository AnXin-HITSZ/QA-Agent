"""Runner orchestration tests: no database, model, or subprocess API calls."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("memory_runner", Path(__file__).parents[1] / "scripts/run_memory_eval.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_full_plan_has_three_modes_large_budget_and_explicit_judge():
    args = SimpleNamespace(action="full", dataset=Path("data/sample.json"), context_chars=100000, no_judge=False)
    commands = runner.plan(args, "sample")
    assert [c[0] for c in commands] == ["build", "ask", "score", "report"]
    assert "no_memory,memory,full_context" in commands[1]
    assert "100000" in commands[1] and "--answer" in commands[1]
    assert "--judge-use-answer-model" in commands[2]
    assert not any(c[0] == "purge" for c in commands)
    args.no_judge = True
    assert runner.plan(args, "sample")[2][-1] == "f1,bleu1"


def test_full_dataset_prepares_all_samples_without_question_limit():
    args = SimpleNamespace(action="full", dataset=Path("runs/full/prepared-dataset.json"),
                           input=Path("locomo10.json"), full_dataset=True,
                           context_chars=100000, no_judge=False)
    commands = runner.plan(args, "full")
    assert [c[0] for c in commands] == ["prepare", "build", "ask", "score", "report"]
    assert commands[0][-2:] == ["--sample-limit", "0"]
    assert "--question-limit" not in commands[0]
    assert commands[0][commands[0].index("--output") + 1] == str(args.dataset)
    assert commands[1][commands[1].index("--dataset") + 1] == str(args.dataset)


def test_full_dataset_dry_run_does_not_touch_smoke_or_services(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(runner, "settings_and_environment", lambda: pytest.fail("loaded services"))
    assert runner.main(["--full-dataset", "--dry-run", "--run-id", "preview-full"]) == 0
    output = capsys.readouterr().out
    assert "prepare" in output and "--sample-limit 0" in output
    assert "prepared-dataset.json" in output and "locomo-smoke.json" not in output
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("options", [
    ["--full-dataset", "--dataset", "sample.json"],
    ["--full-dataset", "--action", "purge"],
    ["--input", "locomo10.json"],
])
def test_invalid_full_dataset_options_stop_before_services(options, monkeypatch):
    monkeypatch.setattr(runner, "settings_and_environment", lambda: pytest.fail("loaded services"))
    assert runner.main([*options, "--dry-run"]) == 1


def test_dry_run_is_zero_io_and_no_service_calls(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(runner, "LATEST", tmp_path / "latest.json")
    monkeypatch.setattr(runner, "settings_and_environment", lambda: pytest.fail("dry-run loaded settings"))
    assert runner.main(["--dry-run", "--run-id", "preview"]) == 0
    assert "100000" in capsys.readouterr().out
    assert not (tmp_path / "runs").exists() and not (tmp_path / "latest.json").exists()


@pytest.mark.parametrize("value", ["../escape", "a/b", "", "a" * 50])
def test_run_id_rejects_paths_and_matches_underlying_limit(value):
    with pytest.raises(ValueError):
        runner.checked_id(value)


def test_latest_id_is_reused_for_status_and_purge(tmp_path, monkeypatch):
    latest = tmp_path / "latest.json"
    runner.write_json(latest, {"run_id": "old-experiment"})
    monkeypatch.setattr(runner, "LATEST", latest)
    assert runner.resolve_id("status", None) == "old-experiment"
    assert runner.resolve_id("purge", None) == "old-experiment"
    assert runner.resolve_id("full", None) != "old-experiment"


def test_failed_build_stops_paid_followups_and_keeps_record(tmp_path, monkeypatch):
    calls = []
    def fake(command, **kwargs):
        calls.append(command)
        assert kwargs["cwd"] == runner.BACKEND
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(runner.subprocess, "run", fake)
    assert runner.execute([["build"], ["ask"], ["score"]], tmp_path, {"run_id": "x"}) == 1
    assert len(calls) == 1
    record = json.loads((tmp_path / "runner.json").read_text(encoding="utf-8"))
    assert record["state"] == "failed" and record["step"] == "build"


def test_full_refuses_to_overwrite_run(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RUNS", tmp_path)
    (tmp_path / "existing").mkdir()
    monkeypatch.setattr(runner, "settings_and_environment", lambda: pytest.fail("should stop before loading settings"))
    assert runner.main(["--run-id", "existing"]) == 1


def test_preflight_rejects_production_before_connecting():
    settings = SimpleNamespace(app_env="prod", memory_collection="user_memory")
    with pytest.raises(ValueError, match="仅用于 dev"):
        runner.preflight(settings, "x")
