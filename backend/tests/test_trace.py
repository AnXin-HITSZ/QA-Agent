"""ReAct 轨迹回读 used_sop_id 的离线单测(不涉及 LLM)。"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.config import get_settings
from app.graph.trace import used_sop_id
from app.skills import loader

_TRAVEL = """---
id: travel
name: 差旅报销
description: 出差交通住宿费用报销
triggers:
  - 差旅
---
# 差旅报销
1. 填报销单
"""

_SUPPLIES = """---
id: supplies
name: 办公用品报销
description: 办公用品采购报销
triggers:
  - 办公用品
---
# 办公用品报销
1. 提交采购申请
"""


def _prepare(tmp_path, monkeypatch):
    (tmp_path / "travel.md").write_text(_TRAVEL, encoding="utf-8")
    (tmp_path / "supplies.md").write_text(_SUPPLIES, encoding="utf-8")
    monkeypatch.setenv("SOPS_DIR", str(tmp_path))
    get_settings.cache_clear()
    loader.reload()


def _get_sop(skill_id: str, cid: str = "c1") -> dict:
    return {"name": "get_sop", "args": {"skill_id": skill_id}, "id": cid, "type": "tool_call"}


def _search(query: str, cid: str = "c0") -> dict:
    return {"name": "search_sops", "args": {"query": query}, "id": cid, "type": "tool_call"}


def test_used_sop_id_from_last_get_sop(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    messages = [
        HumanMessage(content="差旅怎么报销"),
        AIMessage(content="", tool_calls=[_get_sop("travel")]),
        ToolMessage(content="# 差旅报销 ...", tool_call_id="c1"),
        AIMessage(content="第一步:填报销单……"),
    ]
    assert used_sop_id(messages) == "travel"


def test_used_sop_id_none_when_only_search(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    messages = [
        HumanMessage(content="有哪些报销"),
        AIMessage(content="", tool_calls=[_search("报销")]),
        ToolMessage(content="找到以下 SOP……", tool_call_id="c0"),
        AIMessage(content="目前有差旅和办公用品两类"),
    ]
    assert used_sop_id(messages) is None


def test_used_sop_id_none_when_no_tools(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    messages = [HumanMessage(content="打印机在哪"), AIMessage(content="在三楼东侧")]
    assert used_sop_id(messages) is None


def test_used_sop_id_ignores_unknown_id(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    messages = [AIMessage(content="", tool_calls=[_get_sop("nope")])]
    assert used_sop_id(messages) is None


def test_used_sop_id_returns_last_valid(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    messages = [
        AIMessage(content="", tool_calls=[_get_sop("travel", "c1")]),
        ToolMessage(content="# 差旅报销 ...", tool_call_id="c1"),
        AIMessage(content="", tool_calls=[_get_sop("supplies", "c2")]),
        ToolMessage(content="# 办公用品报销 ...", tool_call_id="c2"),
        AIMessage(content="按办公用品流程……"),
    ]
    assert used_sop_id(messages) == "supplies"


def test_used_sop_id_falls_back_past_invalid_last(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    messages = [
        AIMessage(content="", tool_calls=[_get_sop("travel", "c1")]),
        ToolMessage(content="# 差旅报销 ...", tool_call_id="c1"),
        AIMessage(content="", tool_calls=[_get_sop("nope", "c2")]),
        ToolMessage(content="找不到……", tool_call_id="c2"),
        AIMessage(content="按差旅流程……"),
    ]
    assert used_sop_id(messages) == "travel"
