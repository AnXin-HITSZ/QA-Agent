"""Evaluation integrity checks; no Ragas dependency or paid calls."""
import json
from pathlib import Path

from langchain_core.messages import ToolMessage
import pytest

from scripts.rag_eval import aggregate, contexts_from_messages, read_cases, require_review


def test_contexts_use_exact_model_visible_text_not_artifact():
    message = ToolMessage(content="找到以下相关资料:\n\n[1] 来源:a\n截断后的正文\n\n[2] 来源:b\n第二片段",
                          tool_call_id="1", name="search_knowledge",
                          artifact=[{"oss_key": "a", "text": "模型没有看到的完整正文"}])
    contexts, sources, tools, error = contexts_from_messages([message])
    assert contexts == ["[1] 来源:a\n截断后的正文", "[2] 来源:b\n第二片段"]
    assert sources[0]["oss_key"] == "a" and tools == ["search_knowledge"]
    assert not error


def test_no_hit_notice_is_not_evidence():
    message = ToolMessage(content="知识库中没有找到与该问题相关的资料。", name="search_knowledge", tool_call_id="1", artifact=[])
    assert contexts_from_messages([message])[0] == []


def test_tool_failure_and_auxiliary_tools_are_visible():
    failed = ToolMessage(content="知识库当前不可用，无法检索", name="search_knowledge", tool_call_id="1", artifact=[])
    sop = ToolMessage(content="SOP policy", name="get_sop", tool_call_id="2")
    contexts, sources, tools, error = contexts_from_messages([failed, sop])
    assert error and "get_sop" in tools and not contexts


def test_failed_scores_are_not_silently_zero_or_hidden():
    rows = [
        {"metrics": {"m": {"value": .8, "status": "ok"}}},
        {"metrics": {"m": {"value": None, "status": "failed"}}},
        {"metrics": {"m": {"value": None, "status": "not_applicable"}}},
    ]
    stats = aggregate(rows, ["m"])["metrics"]["m"]
    assert stats == {"mean": .8, "valid": 1, "total": 3, "failed": 1, "not_applicable": 1}


def test_duplicate_questions_rejected_before_calls(tmp_path):
    path = tmp_path / "dataset.json"
    row = {"id": "same", "user_input": "q", "reference": "a"}
    path.write_text(json.dumps({"cases": [row, row]}), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        read_cases(path)


def test_sample_is_explicitly_synthetic_and_references_are_present():
    path = Path(__file__).resolve().parents[1] / "scripts/rag_eval.sample.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["synthetic"]
    assert len(read_cases(path)) == 4


def test_unconfirmed_real_dataset_cannot_start_paid_collection():
    with pytest.raises(ValueError, match="人工确认"):
        require_review({"synthetic": False, "review_status": "pending"}, "isolated")
    require_review({"synthetic": False, "review_status": "approved"}, "isolated")


def test_real_data_cannot_bypass_review_using_synthetic_command():
    with pytest.raises(ValueError, match="合成集"):
        require_review({"synthetic": False}, "synthetic")


def test_invalid_expected_source_rejected_before_calls(tmp_path):
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps({"cases": [{"id": "1", "user_input": "q", "reference": "a", "expected_sources": [{}]}]}),encoding="utf-8")
    with pytest.raises(ValueError, match="expected_sources"):
        read_cases(path)
