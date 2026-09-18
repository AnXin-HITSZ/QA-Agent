"""Skill 加载器离线单测(不涉及 LLM)。"""

from app.config import get_settings
from app.skills import loader

_SKILL_MD = """---
id: travel
name: 差旅报销
description: 出差交通住宿费用报销
triggers:
  - 差旅
  - 出差
---
# 差旅报销
1. 填报销单
2. 粘贴发票
"""


def _prepare(tmp_path, monkeypatch, *, filename="travel.md", content=_SKILL_MD):
    (tmp_path / filename).write_text(content, encoding="utf-8")
    monkeypatch.setenv("SOPS_DIR", str(tmp_path))
    get_settings.cache_clear()
    loader.reload()


def test_catalog_and_get_skill(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)

    catalog = loader.get_catalog()
    assert len(catalog) == 1
    item = catalog[0]
    assert item.id == "travel"
    assert item.name == "差旅报销"
    assert "差旅" in item.triggers

    found = loader.get_skill("travel")
    assert found is not None
    meta, body = found
    assert meta.id == "travel"
    assert "填报销单" in body

    assert loader.get_skill("does-not-exist") is None


def test_reload_picks_up_new_file(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    assert len(loader.get_catalog()) == 1

    (tmp_path / "supplies.md").write_text(
        "---\nid: supplies\nname: 耗材报销\ndescription: 耗材试剂报销\n---\n# 耗材\n步骤\n",
        encoding="utf-8",
    )
    # 未 reload 前仍是缓存的 1 个
    assert len(loader.get_catalog()) == 1

    count = loader.reload()
    assert count == 2
    assert {s.id for s in loader.get_catalog()} == {"travel", "supplies"}


def test_id_defaults_to_filename(tmp_path, monkeypatch):
    _prepare(
        tmp_path,
        monkeypatch,
        filename="no-id.md",
        content="---\nname: 无 id 技能\n---\n正文\n",
    )
    found = loader.get_skill("no-id")
    assert found is not None
    assert found[0].name == "无 id 技能"
