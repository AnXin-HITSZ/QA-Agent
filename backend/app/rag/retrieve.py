"""Overfetch, suppress redundant chunks, expand same-version neighbors and return
bounded evidence. Query embeddings never use Redis. Payload filtering is future work.
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.metering.context import PURPOSE_QUERY, bind as bind_context
from app.rag import store
from app.rag.chunking import token_bound
from app.rag.embeddings import get_embeddings

logger = logging.getLogger(__name__)
DEFAULT_TOP_K = 5
SCORE_THRESHOLD = 0.2


def _redundant(a: dict, b: dict) -> bool:
    if a.get("oss_key") != b.get("oss_key") or a.get("section_path", []) != b.get("section_path", []):
        return False
    at, bt = a.get("body", a.get("text", "")).strip(), b.get("body", b.get("text", "")).strip()
    if at == bt or (min(len(at), len(bt)) >= get_settings().rag_duplicate_min_chars and (at in bt or bt in at)):
        return True
    if a.get("offset_version") == b.get("offset_version") == "extracted-v2":
        start, end = max(a["text_start"], b["text_start"]), min(a["text_end"], b["text_end"])
        return max(0, end - start) >= get_settings().rag_duplicate_overlap * max(1, min(a["text_end"] - a["text_start"], b["text_end"] - b["text_start"]))
    return False


def _identity(p: dict) -> tuple:
    return p.get("oss_key"), p.get("content_sha256"), p.get("chunk_index")


def _evidence(parts: list[dict], score: float | None) -> dict:
    parts = sorted(parts, key=lambda p: p.get("chunk_index", 0))
    primary = parts[0]
    body = ""
    previous = None
    for p in parts:
        text = p.get("body") or p.get("text") or ""
        if previous is None:
            body = text
        elif p.get("offset_version") == previous.get("offset_version") == "extracted-v2":
            gap = p["text_start"] - previous["text_end"]
            if gap <= 0:
                body += text[min(len(text), -gap):]
            else:
                body += ("\n" if gap == 1 else "\n\n[片段间有省略]\n\n") + text
        else:
            body += "\n\n" + text
        previous = p
    section = primary.get("section_path") or []
    prefix = ""
    if "context_prefix" in primary:
        prefix = primary["context_prefix"]
    elif primary.get("body"):
        prefix = f"来源: {primary.get('oss_key') or primary.get('source') or ''}\n"
        if section:
            prefix += "章节: " + " > ".join(section) + "\n"
        prefix += "\n"
    body = (primary.get("frame_header") or "") + body + (parts[-1].get("frame_footer") or "")
    return {"text": prefix + body, "oss_key": primary.get("oss_key"), "source": primary.get("source"),
            "category": primary.get("category"), "score": score, "ext": primary.get("ext"),
            "chunk_index": primary.get("chunk_index"),
            "chunk_indices": sorted({p["chunk_index"] for p in parts if isinstance(p.get("chunk_index"), int)}),
            "page_numbers": sorted({n for p in parts for n in p.get("page_numbers", []) if isinstance(n, int) and n > 0}),
            "section_path": section, "content_sha256": primary.get("content_sha256"), "truncated": False}


def search_knowledge(query: str, top_k: int | None = None,
                     score_threshold: float | None = None) -> list[dict]:
    q = (query or "").strip()
    s = get_settings()
    top_k = s.rag_top_k if top_k is None else top_k
    score_threshold = s.rag_score_threshold if score_threshold is None else score_threshold
    if not q or top_k <= 0:
        return []
    top_k = min(top_k, 20)
    with bind_context(purpose=PURPOSE_QUERY, job_id=None, document_id=None, oss_key=None, page_no=None):
        vector = get_embeddings().embed_query(q)
    collection = store.ensure_collection()
    points = store.search(vector, top_k=min(200, max(top_k, s.rag_retrieval_candidates)), collection=collection)
    anchors: list[tuple[dict, float | None]] = []
    for point in points:
        p = point.payload or {}
        if (point.score is not None and point.score < score_threshold) or not (p.get("text") or "").strip():
            continue
        if any(_redundant(p, chosen) for chosen, _ in anchors):
            continue
        anchors.append((p, point.score))
        if len(anchors) >= top_k:
            break
    groups: list[dict] = []
    remaining_chars, remaining_tokens = s.rag_context_chars, s.rag_context_tokens
    # Reserve primary evidence before neighbors, protecting other relevant hits.
    for p, score in anchors:
        evidence = _evidence([p], score)
        text = evidence["text"]
        if len(text) > remaining_chars or token_bound(text) > remaining_tokens:
            end = min(len(text), remaining_chars)
            while end and token_bound(text[:end]) > remaining_tokens:
                end -= 1
            if end <= 0:
                break
            evidence["text"] = text[:end]
            evidence["truncated"] = True
        remaining_chars -= len(evidence["text"])
        remaining_tokens -= token_bound(evidence["text"])
        groups.append({"parts": [p], "score": score, "evidence": evidence})
    claimed = {_identity(p) for g in groups for p in g["parts"]}
    for g in groups:
        if g["evidence"]["truncated"]:
            continue
        try:
            neighbors = store.adjacent_chunks(g["parts"][0], collection=collection, radius=s.rag_neighbor_radius)
        except Exception as exc:
            logger.warning("邻块补取失败，本轮保留主命中: %s", type(exc).__name__)
            continue
        for p in sorted(neighbors, key=lambda n: abs(n["chunk_index"] - g["parts"][0]["chunk_index"])):
            if _identity(p) in claimed or not (p.get("text") or "").strip():
                continue
            evidence = _evidence(g["parts"] + [p], g["score"])
            added_chars = len(evidence["text"]) - len(g["evidence"]["text"])
            added_tokens = token_bound(evidence["text"]) - token_bound(g["evidence"]["text"])
            if added_chars <= remaining_chars and added_tokens <= remaining_tokens:
                g["parts"].append(p)
                g["evidence"] = evidence
                remaining_chars -= added_chars
                remaining_tokens -= added_tokens
                claimed.add(_identity(p))
    # Merge adjacent evidence groups from the same section, removing overlap once.
    merged: list[dict] = []
    # Process by chunk order inside each document, then restore relevance order.
    groups.sort(key=lambda g: (g["parts"][0].get("oss_key") or "",
                              str(g["parts"][0].get("section_path", [])),
                              min(p.get("chunk_index", 0) for p in g["parts"])))
    for group in groups:
        if group["evidence"]["truncated"]:
            merged.append(group)
            continue
        for other in merged:
            a, b = other["parts"][0], group["parts"][0]
            ai = {p.get("chunk_index") for p in other["parts"]}
            bi = {p.get("chunk_index") for p in group["parts"]}
            if (not other["evidence"]["truncated"] and a.get("oss_key") == b.get("oss_key")
                and a.get("content_sha256") == b.get("content_sha256")
                and a.get("section_path", []) == b.get("section_path", [])
                and a.get("offset_version") == b.get("offset_version") == "extracted-v2"
                and any(isinstance(i, int) and i + d in bi for i in ai for d in (-1, 0, 1))):
                parts = {_identity(p): p for p in other["parts"] + group["parts"]}
                evidence = _evidence(list(parts.values()), max(other["score"] or 0, group["score"] or 0))
                if len(evidence["text"]) <= len(other["evidence"]["text"]) + len(group["evidence"]["text"]) and token_bound(evidence["text"]) <= token_bound(other["evidence"]["text"]) + token_bound(group["evidence"]["text"]):
                    other.update(parts=list(parts.values()), evidence=evidence, score=evidence["score"])
                    break
        else:
            merged.append(group)
    return [g["evidence"] for g in sorted(merged, key=lambda g: g["score"] or 0, reverse=True)]


def format_hits(hits: list[dict]) -> str:
    s = get_settings()
    blocks: list[str] = []
    remaining_chars, remaining_tokens = s.rag_context_chars, s.rag_context_tokens
    marker = "\n[上下文预算不足，片段已截断]"
    for i, h in enumerate(hits, start=1):
        src = h.get("source") or h.get("oss_key") or "未知来源"
        pages = h.get("page_numbers") or []
        location = "；页码:" + ", ".join(map(str, pages)) if pages else ""
        section = "；章节:" + " > ".join(h["section_path"]) if h.get("section_path") else ""
        note = marker if h.get("truncated") else ""
        block = f"[{i}] 来源:{src}{location}{section}\n{h.get('text') or ''}{note}"
        separator = "\n\n" if blocks else ""
        if len(separator + block) > remaining_chars or token_bound(separator + block) > remaining_tokens:
            h["truncated"] = True
            from app.rag.chunking import _end
            suffix = marker
            room_chars = remaining_chars - len(separator + suffix)
            room_tokens = remaining_tokens - token_bound(separator + suffix)
            if room_chars <= 0 or room_tokens <= 0:
                break
            end = _end(block, 0, room_chars, room_tokens)
            block = block[:end] + suffix
        blocks.append(block)
        remaining_chars -= len(separator + block)
        remaining_tokens -= token_bound(separator + block)
        if h.get("truncated") and (remaining_chars <= len(marker) or remaining_tokens <= token_bound(marker)):
            break
    return "\n\n".join(blocks)
