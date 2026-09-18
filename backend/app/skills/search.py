"""SOP 全文关键词检索(第 4 步 RAG 之前的确定性实现)。

纯函数:吃 loader 的缓存数据,按字段加权打分返回候选。
这里是「检索」的接缝——第 4 步接 pgvector 时,把关键词打分换成向量检索即可,
上层(工具、图)签名不变。不涉及 LLM,可脱机单测。
"""

from __future__ import annotations

import re

from app.schemas.skill import SopHit
from app.skills import loader

# 字段权重:名称 / 触发词 > id > 简介 > 正文。
_W_NAME = 5
_W_TRIGGER = 4
_W_ID = 3
_W_DESC = 2
_W_BODY = 1

_WS = re.compile(r"\s+")


def _terms(query: str) -> list[str]:
    """查询词:按空白切分并转小写(中文大小写无关,拉丁需归一)。"""
    return [t for t in _WS.split(query.strip().lower()) if t]


def _snippet(body: str, terms: list[str], width: int = 80) -> str:
    """取正文中首个命中词周边一小段;无命中则取开头一段。"""
    text_lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    joined = " ".join(text_lines)
    if not joined:
        return ""
    low = joined.lower()
    for t in terms:
        i = low.find(t)
        if i != -1:
            start = max(0, i - width // 3)
            seg = joined[start : start + width].strip()
            return ("…" if start > 0 else "") + seg + ("…" if start + width < len(joined) else "")
    return joined[:width].strip() + ("…" if len(joined) > width else "")


def search_sops(query: str, top_k: int = 5) -> list[SopHit]:
    """按相关度检索 SOP。

    query 为空 → 列出全部(按名称升序);有词但完全不命中的 SOP 会被排除。
    返回按相关度降序、至多 top_k 条。
    """
    terms = _terms(query)
    k = max(1, top_k)
    scored: list[tuple[int, SopHit]] = []

    for meta in loader.get_catalog():
        found = loader.get_skill(meta.id)
        body = found[1] if found else ""
        fields = {
            "name": (meta.name or "").lower(),
            "trigger": " ".join(meta.triggers).lower(),
            "id": meta.id.lower(),
            "desc": (meta.description or "").lower(),
            "body": body.lower(),
        }
        score = 0
        for t in terms:
            if t in fields["name"]:
                score += _W_NAME
            if t in fields["trigger"]:
                score += _W_TRIGGER
            if t in fields["id"]:
                score += _W_ID
            if t in fields["desc"]:
                score += _W_DESC
            if t in fields["body"]:
                score += _W_BODY

        if terms and score == 0:
            continue  # 有查询词却完全不命中 → 不返回

        scored.append(
            (
                score,
                SopHit(
                    id=meta.id,
                    name=meta.name,
                    description=meta.description,
                    score=score,
                    snippet=_snippet(body, terms),
                ),
            )
        )

    if terms:
        scored.sort(key=lambda x: x[0], reverse=True)  # 相关度降序
    else:
        scored.sort(key=lambda x: x[1].name)  # 空查询:按名称列全部

    return [hit for _, hit in scored[:k]]
