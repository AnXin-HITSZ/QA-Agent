"""Complete literal set A plus bounded lexical/vector supplement.

Only supplement files are reranked. A is path-sorted and never score-filtered.
Persistent results are owner-scoped; original existence/version is rechecked on
each page/export. Query-time metadata enumeration never downloads file bodies.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import hashlib

from app.config import get_settings
from app.rag import fulltext, oss, search_repo, store
from app.schemas.file_search import FileSearchRequest


def literal_matches(values, terms, operator):
    tests = [any(term in value for value in values) for term in terms]
    return all(tests) if operator == "all" else any(tests)


def current_version(doc, inventory):
    info = inventory.get(doc["key"])
    return bool(info and info.get("size") == doc.get("source_size")
                and info.get("last_modified") == doc.get("source_modified"))


def evidence(doc, terms):
    result = []
    units = doc.get("units") or [{"text": doc.get("body", "")}]
    for unit in units:
        text = unit["text"]
        positions = [text.find(t) for t in terms if t in text]
        if positions:
            pos = min(positions)
            result.append({"text": text[max(0, pos - 80):pos + 320], "page": unit.get("page"),
                           "section": unit.get("section", [])})
            if len(result) == 3:
                break
    if not result and doc.get("body"):
        result = [{"text": doc["body"][:400], "page": None, "section": []}]
    return result


def item(doc, routes, terms, snippets=None):
    values = [doc.get("name", ""), doc["key"], doc.get("body", "")]
    return {"key": doc["key"], "name": doc.get("name") or doc["key"].rsplit("/", 1)[-1],
        "routes": routes, "matched_terms": [t for t in terms if any(t in v for v in values)],
        "evidence": snippets if snippets is not None else evidence(doc, terms),
        "content_sha256": doc.get("content_sha256"), "source_size": doc.get("source_size"),
        "source_modified": doc.get("source_modified"), "body_indexed": bool(doc.get("content_sha256"))}


def vector_candidates(query, terms, collection, prefix, limit, allowed_keys):
    from app.rag.embeddings import get_embeddings
    from app.metering.context import bind, PURPOSE_QUERY
    from qdrant_client.models import FieldCondition, Filter, MatchAny
    candidates = []
    for expression in dict.fromkeys([query, *terms]):
        with bind(purpose=PURPOSE_QUERY):
            vector = get_embeddings().embed_query(expression)
        flt = Filter(must=[FieldCondition(key="oss_key", match=MatchAny(any=allowed_keys))]) if prefix else None
        groups = store.get_client().query_points_groups(collection_name=collection, query=vector,
            group_by="oss_key", group_size=3, limit=limit, query_filter=flt,
            with_payload=True, score_threshold=get_settings().file_search_vector_threshold).groups
        for group in groups:
            parts = [point.payload for point in group.hits if point.payload]
            if not parts:
                continue
            p = parts[0]
            candidates.append({"key": p["oss_key"], "name": p.get("source"),
                "content_sha256": p.get("content_sha256"),
                "evidence": [{"text": (part.get("body") or part.get("text", ""))[:1200],
                    "page": (part.get("page_numbers") or [None])[0], "section": part.get("section_path", [])} for part in parts]})
    return candidates


def supplements(req, release, inventory, protected):
    s = get_settings()
    notes = []
    candidates, ranks = {}, defaultdict(float)
    allowed = [f["key"] for f in release["files"] if f["key"].startswith(req.prefix)]
    lexical_limit = req.candidate_limit or s.file_search_fulltext_candidates
    vector_limit = req.candidate_limit or s.file_search_vector_candidates
    def lexical_run():
        hits = fulltext.lexical(release["es_index"], req.query, req.prefix, lexical_limit)
        return [(h["_source"], [{"text": t, "page": None, "section": []}
                 for t in h.get("highlight", {}).get("body", [])]) for h in hits]
    if not allowed:
        return [], notes
    # Each worker binds its own metering context; DB sessions never cross threads.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [("fulltext", pool.submit(lexical_run)), ("vector", pool.submit(vector_candidates,
            req.query, req.literal_terms, release["id"], req.prefix, vector_limit, allowed))]
        files = {f["key"]: f for f in release["files"]}
        for route, future in futures:
            try:
                rows = future.result()
            except Exception as exc:
                notes.append(f"{route} 补充召回失败（{type(exc).__name__}），字面匹配仍保留")
                continue
            seen = set()
            for rank, row in enumerate(rows, 1):
                doc, snippets = row if route == "fulltext" else (row, row["evidence"])
                key = doc["key"]
                meta = files.get(key)
                if key in protected or key in seen or not meta or not current_version(meta, inventory):
                    continue
                seen.add(key)
                ranks[key] += 1 / (60 + rank)
                if key not in candidates:
                    candidates[key] = item({**doc, **meta}, [route], req.literal_terms, snippets or evidence(doc, req.literal_terms))
                else:
                    candidates[key]["routes"].append(route)
                    candidates[key]["evidence"] = (candidates[key]["evidence"] + snippets)[:6]
    ordered = sorted(candidates.values(), key=lambda r: (-ranks[r["key"]], r["key"]))
    cap = s.file_search_rerank_candidates
    ordered = ordered[:cap]
    if not ordered:
        return [], notes
    from app.memory import rerank
    from app.metering.context import bind, PURPOSE_QUERY
    if not rerank.configured():
        notes.append("重排序未配置，补充结果使用 RRF 排序")
        return ordered, notes
    per_document = min(s.file_search_rerank_chars, s.file_search_rerank_total_chars // len(ordered))
    docs = [(row["key"][:200] + "\n" + "\n".join(e["text"] for e in row["evidence"]))[:per_document] for row in ordered]
    with bind(purpose=PURPOSE_QUERY):
        outcome = rerank.rerank(req.query, docs, top_n=len(docs))
    if not outcome.ok:
        notes.append("重排序失败，补充结果使用 RRF 排序")
        return ordered, notes
    ranked = []
    for i in outcome.order:
        if outcome.scores[i] >= s.file_search_rerank_threshold:
            row = ordered[i]
            row["score"] = outcome.scores[i]
            ranked.append(row)
    # Partial provider results are explicitly degraded, not silently lost.
    if len(outcome.order) != len(ordered):
        notes.append("重排序部分结果缺失，缺失候选保留融合顺序")
        ranked.extend(row for i, row in enumerate(ordered) if i not in outcome.order)
    return ranked, notes


def search(req: FileSearchRequest, user_id):
    if not fulltext.enabled():
        raise fulltext.SearchUnavailable("文件搜索尚未启用")
    release = search_repo.current()
    if not release:
        raise fulltext.SearchUnavailable("尚未发布联合全文/向量索引，请先执行全库索引")
    inventory = {f["key"]: f for f in oss.knowledge_store().list_all(req.prefix)}
    indexed = {f["key"]: f for f in release["files"] if f["key"] in inventory and current_version(f, inventory)}
    # Names of newly uploaded/unindexed files are searchable from source metadata.
    literal = {}
    if "name" in req.fields or "path" in req.fields:
        for key, info in inventory.items():
            values = ([key.rsplit("/", 1)[-1]] if "name" in req.fields else []) + ([key] if "path" in req.fields else [])
            if literal_matches(values, req.literal_terms, req.literal_operator):
                doc = {"key": key, "name": key.rsplit("/", 1)[-1],
                       "source_size": info["size"], "source_modified": info.get("last_modified"), **indexed.get(key, {})}
                literal[key] = item(doc, ["literal"], req.literal_terms, [])
    for doc in fulltext.scan(release["es_index"], fulltext.literal_query(req.literal_terms, req.literal_operator, req.fields), req.prefix):
        if not current_version(doc, inventory):
            continue
        literal[doc["key"]] = item(doc, ["literal"], req.literal_terms)
    extra, notes = supplements(req, release, inventory, set(literal)) if req.mode == "semantic" else ([], [])
    metadata = {"mode": req.mode, "query": req.query, "literal_terms": req.literal_terms,
        "literal_operator": req.literal_operator, "fields": req.fields, "prefix": req.prefix,
        "literal_total": len(literal), "supplement_total": len(extra), "total": len(literal) + len(extra),
        "coverage": {"source_files": len(inventory), "body_indexed": len(indexed), "body_uncovered": len(inventory) - len(indexed)},
        "notes": notes, "semantic_exhaustive": False, "literal_complete": True,
        "release_id": release["id"]}
    sid = search_repo.new_session(user_id, release["id"], metadata)
    try:
        rows = [literal[k] for k in sorted(literal)] + extra
        for start in range(0, len(rows), 200):
            search_repo.append_items(sid, start, rows[start:start + 200])
        search_repo.finish(sid, metadata)
    except Exception:
        search_repo.remove_session(sid)
        raise
    return page(sid, user_id, 0, req.page_size)


def page(sid, user_id, offset=0, size=20):
    if offset < 0 or not 1 <= size <= 50:
        raise ValueError("分页参数非法")
    metadata, rows = search_repo.read_page(sid, user_id, offset, size)
    # Revalidation prevents stale result-set content/links after deletion/replacement.
    kb = oss.knowledge_store()
    unavailable = 0
    safe_rows = []
    for row in rows:
        info = kb.stat(row["key"])
        if not info or info.get("size") != row.get("source_size") or info.get("last_modified") != row.get("source_modified"):
            unavailable += 1
            continue
        safe_rows.append(row)
    next_offset = offset + len(rows)
    return {**metadata, "search_id": sid, "items": safe_rows, "offset": offset,
        "page_size": size,
        "unavailable_on_page": unavailable, "next_offset": next_offset if next_offset < metadata["total"] else None}
