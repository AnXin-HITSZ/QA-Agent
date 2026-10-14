"""Resolve policies from the active version; missing/conflicting legacy data stays explicit."""
from __future__ import annotations
import hashlib
import json
from pathlib import PurePosixPath
from app.config import get_settings

from app.rag import documents, job_repo, oss, search_repo, store
from app.rag.cache_store import get_cache
from app.rag.document_extract import IMAGE_EXTS
from app.rag.extract import SUPPORTED_EXTS
from app.schemas.knowledge import IndexOptions


def options_of(row):
    raw = row.get("extraction_options") or row
    try:
        return IndexOptions(extraction_mode=raw["extraction_mode"],
                            mixed_invoice=raw.get("mixed_invoice", False), refresh_ocr=False).model_dump()
    except (ValueError, KeyError, TypeError):
        return None


def active_policies(active):
    if job_repo.configured():
        saved = job_repo.policies(active)
        if saved:
            return saved
    release = search_repo.current() if get_settings().file_search_enabled else None
    if release and release["id"] == active:
        return {f["key"]: f for f in release["files"]}
    return {}


def legacy_policies(active):
    # Copied chunks retain their original index_version: validate against that,
    # rather than trusting a later Redis manifest after a rollback.
    versions = {}
    offset = None
    if not store.collection_exists(active):
        return {}
    while True:
        points, offset = store.get_client().scroll(collection_name=active, limit=200,
            offset=offset, with_payload=True, with_vectors=False)
        for point in points:
            p = point.payload or {}
            if p.get("oss_key"):
                versions.setdefault(p["oss_key"], set()).add((p.get("index_version"), p.get("content_sha256")))
        if offset is None:
            break
    result = {}
    for key, refs in versions.items():
        if len(refs) != 1:
            continue
        version, content = next(iter(refs))
        doc_id = get_cache().get(documents.path_key(key))
        manifest = documents.manifest_of(doc_id) if doc_id else None
        if manifest and manifest.get("oss_key") == key and manifest.get("index_version") == version and manifest.get("content_sha256") == content:
            result[key] = {"key": key, "document_id": doc_id, "content_sha256": content,
                           "extraction_options": options_of(manifest), "policy_source": "legacy_manifest"}
    # Archived successful jobs can fill missing manifests, but only the exact
    # chunk generation and successful file result qualify; no newest-time guessing.
    if job_repo.configured():
        offset = 0
        while True:
            page = job_repo.list_page(offset, 100, "published")
            for job in page["items"]:
                version = (job.get("summary") or {}).get("index_version")
                count = job_repo.files(job["job_id"], 0, 1)["total"]
                for start in range(0, count, 200):
                    for row in job_repo.files(job["job_id"], start, 200)["items"]:
                        key = row.get("key")
                        refs = versions.get(key, set())
                        if key in result or row.get("status") != "indexed" or len(refs) != 1 or next(iter(refs))[0] != version:
                            continue
                        opts = options_of(row.get("extraction_options") or job.get("options") or {})
                        current_id = get_cache().get(documents.path_key(key))
                        if current_id and row.get("document_id") != current_id:
                            continue
                        if opts:
                            result[key] = {"key": key, "content_sha256": next(iter(refs))[1],
                                           "document_id": row.get("document_id"),
                                           "extraction_options": opts, "policy_source": "legacy_task"}
            offset += len(page["items"])
            if offset >= page["total"]:
                break
    return result


def plan(scope):
    from app.rag.ingest import scope_files
    active = store.active_collection()
    saved = active_policies(active)
    legacy = legacy_policies(active) if not saved or any(not options_of(f) for f in saved.values()) else {}
    targets, missing = scope_files(oss.knowledge_store(), scope)
    rows = []
    groups = {}
    for info in targets:
        key = info["key"]
        policy = saved.get(key) or {}
        if not options_of(policy):
            policy = legacy.get(key) or policy
        opts = options_of(policy)
        current_id = get_cache().get(documents.path_key(key))
        if policy.get("document_id") and current_id != policy["document_id"]:
            opts = None
        unsupported = PurePosixPath(key).suffix.lower().lstrip('.') not in (SUPPORTED_EXTS | IMAGE_EXTS) - {"zip"}
        status = "unsupported" if unsupported else ("known" if opts else "unknown")
        if opts and not unsupported:
            group = opts["extraction_mode"] + ("+mixed_invoice" if opts["mixed_invoice"] else "")
            groups[group] = groups.get(group, 0) + 1
        rows.append({**info, "status": status, "extraction_options": opts,
                     "policy_source": policy.get("policy_source", "published_version") if opts else None})
    rows.extend({**f,"status":"missing","extraction_options":None} for f in missing)
    raw = {"active": active, "scope": scope.as_dict(), "files": rows}
    token = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {**raw, "token": token, "total": len(rows), "groups": groups,
            "unknown": sum(r["status"] in ("unknown", "missing") for r in rows),
            "unsupported": sum(r["status"] == "unsupported" for r in rows)}


def resolve(preview, overrides):
    rows = {r["key"]: r for r in preview["files"]}
    if len({o.key for o in overrides}) != len(overrides):
        raise ValueError("重复的文件配置")
    selected = {}
    for override in overrides:
        if override.key not in rows:
            raise ValueError("补充配置文件不在重建范围内")
        selected[override.key] = override.options.model_dump()
    for key, row in rows.items():
        if row["status"] == "missing":
            raise ValueError("重建范围包含已不存在的文件，请重新选择")
        if key not in selected:
            if row["extraction_options"]:
                selected[key] = row["extraction_options"]
            elif row["status"] == "unsupported":
                # Unsupported extraction is explicitly reported, never inferred as a supported mode.
                selected[key] = {"extraction_mode":"native_only","mixed_invoice":False,"refresh_ocr":False}
            else:
                raise ValueError("有文件缺少历史提取配置，请先补充选择")
    return selected
