"""SOP 检索纯函数 + 工具单测(不涉及 LLM)。"""

from app.config import get_settings
from app.graph.tools import get_sop, search_sops as search_sops_tool
from app.skills import loader
from app.skills.search import search_sops

_TRAVEL = """---
id: travel
name: 差旅报销
description: 出差交通住宿费用报销
triggers:
  - 差旅
  - 出差
  - 高铁
---
# 差旅报销
1. 填报销单
2. 粘贴火车票
"""

_SUPPLIES = """---
id: supplies
name: 办公用品报销
description: 办公用品与耗材采购报销
triggers:
  - 办公用品
  - 耗材
---
# 办公用品报销
1. 提交采购申请
2. 附发票
"""


def _prepare(tmp_path, monkeypatch):
    (tmp_path / "travel.md").write_text(_TRAVEL, encoding="utf-8")
    (tmp_path / "supplies.md").write_text(_SUPPLIES, encoding="utf-8")
    monkeypatch.setenv("SOPS_DIR", str(tmp_path))
    get_settings.cache_clear()
    loader.reload()


def test_search_ranks_by_relevance(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    hits = search_sops("高铁")
    assert hits
    assert hits[0].id == "travel"  # 命中触发词,排最前
    assert hits[0].score > 0
    assert {h.id for h in hits} == {"travel"}  # supplies 不命中,被排除


def test_search_empty_lists_all(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    hits = search_sops("")
    assert {h.id for h in hits} == {"travel", "supplies"}


def test_search_no_match_returns_empty(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    assert search_sops("量子计算机维修") == []


def test_search_top_k_limits(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    assert len(search_sops("", top_k=1)) == 1


def test_search_tool_text_output(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    out = search_sops_tool.invoke({"query": "高铁"})
    assert "id: travel" in out
    empty = search_sops_tool.invoke({"query": "不存在的东西"})
    assert "没有找到" in empty


def test_get_sop_tool(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    out = get_sop.invoke({"skill_id": "travel"})
    assert "差旅报销" in out and "填报销单" in out
    missing = get_sop.invoke({"skill_id": "nope"})
    assert "找不到" in missing
