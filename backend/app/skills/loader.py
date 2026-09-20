"""从阿里云 OSS 的 sops/ 前缀读取 *.md,解析 frontmatter,提供轻量目录与 SOP 全文(带缓存)。

SOP 原件与知识库同桶、分属不同前缀(见 app/rag/oss.py 的 sops_store):list_all 拿 .md
清单 → get_object 取正文 → frontmatter 解析。全部只读。

优雅降级:OSS 未配置(get_bucket 抛 RuntimeError)/ 不可达时返回空目录(get_catalog 空、
get_skill None),不抛异常打断上层——图里的 search_sops / get_sop 工具据此各自返回「没找到」
提示,不致命。单篇坏文件只跳过并告警,不影响整表(同摄取管线的降级风格)。
"""

from __future__ import annotations

import logging
from functools import lru_cache

import frontmatter

from app.rag import oss
from app.schemas.skill import SkillMeta

logger = logging.getLogger(__name__)


def _parse(key: str, raw: str) -> tuple[str, SkillMeta, str]:
    """把单篇 SOP 原文解析成 (skill_id, SkillMeta, 正文)。

    key 为 SOP 前缀相对(如 'travel.md');id 缺省取去扩展的文件名。
    """
    post = frontmatter.loads(raw)
    meta = post.metadata or {}
    stem = key.rsplit("/", 1)[-1]
    if stem.endswith(".md"):
        stem = stem[:-3]
    skill_id = str(meta.get("id") or stem)
    sm = SkillMeta(
        id=skill_id,
        name=str(meta.get("name") or skill_id),
        description=str(meta.get("description") or ""),
        triggers=[str(t) for t in (meta.get("triggers") or [])],
    )
    return skill_id, sm, post.content


@lru_cache
def _load() -> dict[str, tuple[SkillMeta, str]]:
    """返回 {skill_id: (SkillMeta, 正文)}。OSS 未配置 / 不可达 → 空(优雅降级)。"""
    result: dict[str, tuple[SkillMeta, str]] = {}
    store = oss.sops_store()
    try:
        objs = store.list_all()  # 命中 get_bucket:OSS 未配 → RuntimeError,下面兜住
    except Exception as exc:  # noqa: BLE001 —— 未配置 / 网络等一律降级为空目录,不打断上层
        logger.warning("加载 SOP 目录失败,降级为空:%s", exc)
        return result
    for obj in sorted(objs, key=lambda o: o["key"]):
        key = obj["key"]
        if not key.endswith(".md"):
            continue  # sops/ 前缀下只认 .md,忽略其它对象
        try:
            raw = store.get_object(key).decode("utf-8")
            skill_id, sm, body = _parse(key, raw)
        except Exception as exc:  # noqa: BLE001 —— 单篇坏文件跳过并告警,不影响整表
            logger.warning("解析 SOP %s 失败,跳过:%s", key, exc)
            continue
        result[skill_id] = (sm, body)
    return result


def get_catalog() -> list[SkillMeta]:
    """轻量目录:仅元信息,供 LLM 选择 Skill。"""
    return [meta for meta, _ in _load().values()]


def get_skill(skill_id: str) -> tuple[SkillMeta, str] | None:
    """按 id 取 (元信息, SOP 正文);不存在返回 None。"""
    return _load().get(skill_id)


def reload() -> int:
    """清缓存并从 OSS 重扫,返回当前 Skill 数量。"""
    _load.cache_clear()
    return len(_load())
