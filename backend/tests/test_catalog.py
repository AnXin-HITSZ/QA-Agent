"""SOP 目录工具测试:全部元信息交给模型选择,正文按需返回。"""

from app.graph.tools import TOOLS, get_sop, list_sops

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

_FILES = {"travel.md": _TRAVEL, "supplies.md": _SUPPLIES}


def test_list_all_metadata_without_body(install_sops):
    install_sops(_FILES)
    out = list_sops.invoke({})
    for value in ("id: travel", "差旅报销", "出差交通住宿费用报销", "高铁",
                  "id: supplies", "办公用品与耗材采购报销", "耗材"):
        assert value in out
    assert "填报销单" not in out
    assert "提交采购申请" not in out


def test_list_has_no_top_k_limit(install_sops):
    install_sops({f"sop-{i}.md": f"---\nid: sop-{i}\nname: 流程{i}\n---\n正文{i}" for i in range(8)})
    out = list_sops.invoke({})
    for i in range(8):
        assert f"id: sop-{i}" in out
        assert f"正文{i}" not in out


def test_empty_catalog(install_sops):
    install_sops({})
    assert "当前没有可用的 SOP" in list_sops.invoke({})


def test_tool_registration_and_no_arguments():
    names = {tool.name for tool in TOOLS}
    assert "list_sops" in names
    assert "search_sops" not in names
    assert list_sops.args == {}


def test_selected_body_uses_cached_catalog(install_sops, monkeypatch):
    store = install_sops(_FILES)
    list_sops.invoke({})
    def unexpected_download(key):
        raise AssertionError("缓存命中时不应重新下载")
    monkeypatch.setattr(store, "get_object", unexpected_download)
    out = get_sop.invoke({"skill_id": "travel"})
    assert "填报销单" in out
    assert "提交采购申请" not in out
    missing = get_sop.invoke({"skill_id": "nope"})
    assert "找不到" in missing and "list_sops" in missing
