"""扫描 sops/*.md,解析 frontmatter,提供轻量目录与 SOP 全文(带缓存)。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import frontmatter

from app.config import get_settings
from app.schemas.skill import SkillMeta

# 默认目录:backend/sops(loader.py 位于 backend/app/skills/)
_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "sops"


def _sops_dir() -> Path:
    s = get_settings()
    return Path(s.sops_dir) if s.sops_dir else _DEFAULT_DIR


@lru_cache
def _load() -> dict[str, tuple[SkillMeta, str]]:
    """返回 {skill_id: (SkillMeta, 正文)}。目录不存在则为空。"""
    result: dict[str, tuple[SkillMeta, str]] = {}
    d = _sops_dir()
    if not d.exists():
        return result
    for path in sorted(d.glob("*.md")):
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
        meta = post.metadata or {}
        skill_id = str(meta.get("id") or path.stem)
        sm = SkillMeta(
            id=skill_id,
            name=str(meta.get("name") or skill_id),
            description=str(meta.get("description") or ""),
            triggers=[str(t) for t in (meta.get("triggers") or [])],
        )
        result[skill_id] = (sm, post.content)
    return result


def get_catalog() -> list[SkillMeta]:
    """轻量目录:仅元信息,供 LLM 选择 Skill。"""
    return [meta for meta, _ in _load().values()]


def get_skill(skill_id: str) -> tuple[SkillMeta, str] | None:
    """按 id 取 (元信息, SOP 正文);不存在返回 None。"""
    return _load().get(skill_id)


def reload() -> int:
    """清缓存并重扫,返回当前 Skill 数量。"""
    _load.cache_clear()
    return len(_load())
