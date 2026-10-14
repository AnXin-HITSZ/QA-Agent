"""Elasticsearch REST adapter: exact substring and CJK lexical retrieval are separate.

Immutable published indices make search_after stable without an expiring PIT.
Never truncate full bodies; explicit failures prevent publication.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import quote

import httpx

from app.config import get_settings


class SearchUnavailable(RuntimeError):
    pass


def enabled():
    return get_settings().file_search_enabled


def request(method, path, body=None):
    s = get_settings()
    if not enabled() or not s.elasticsearch_url:
        raise SearchUnavailable("文件搜索未接通，请配置 Elasticsearch 并发布全文索引")
    headers = {}
    auth = None
    if s.elasticsearch_api_key:
        headers["Authorization"] = "ApiKey " + s.elasticsearch_api_key
    elif s.elasticsearch_username:
        auth = (s.elasticsearch_username, s.elasticsearch_password)
    import ssl
    verify = ssl.create_default_context(cafile=s.elasticsearch_ca_certs or None)
    try:
        with httpx.Client(timeout=s.elasticsearch_timeout_seconds, headers=headers,
                          auth=auth, verify=verify, trust_env=False) as client:
            response = client.request(method, s.elasticsearch_url.rstrip("/") + path, json=body)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchUnavailable(f"全文搜索服务调用失败（{type(exc).__name__}）") from None
    if isinstance(data, dict) and (data.get("timed_out") or data.get("_shards", {}).get("failed", 0)):
        raise SearchUnavailable("全文搜索未完整执行，不能返回查全结果")
    return data


def index_path(name):
    return "/" + quote(name, safe="")


def create_index(collection):
    prefix = get_settings().elasticsearch_index_prefix
    name = f"{prefix}-{hashlib.sha256(collection.encode()).hexdigest()[:24]}"
    request("PUT", index_path(name), {"settings": {"number_of_shards": 1}, "mappings": {
        "dynamic": "strict", "properties": {
            "file_id": {"type": "keyword"}, "key": {"type": "keyword"},
            "name": {"type": "text", "analyzer": "cjk"},
            "name_literal": {"type": "wildcard"}, "key_literal": {"type": "wildcard"},
            "body": {"type": "text", "analyzer": "cjk"},
            "body_literal": {"type": "wildcard", "ignore_above": 2147483647},
            "content_sha256": {"type": "keyword"}, "snapshot_key": {"type": "keyword"},
            "units": {"type": "object", "enabled": False},
            "source_size": {"type": "long"}, "source_modified": {"type": "long"},
        }}})
    return name


def put_document(index, doc):
    request("PUT", index_path(index) + "/_doc/" + doc["file_id"], doc)


def remove_index(index):
    request("DELETE", index_path(index))


def refresh(index):
    request("POST", index_path(index) + "/_refresh")


def escape_literal(term):
    return "".join("\\" + c if c in "\\*?" else c for c in term)


def literal_query(terms, operator, fields):
    mapping = {"body": "body_literal", "name": "name_literal", "path": "key_literal"}
    clauses = [{"bool": {"should": [{"wildcard": {mapping[f]: {
        "value": "*" + escape_literal(t) + "*"}}} for f in fields], "minimum_should_match": 1}} for t in terms]
    return {"bool": {"must": clauses}} if operator == "all" else {"bool": {"should": clauses, "minimum_should_match": 1}}


def scan(index, query=None, prefix=""):
    conditions = [query or {"match_all": {}}]
    if prefix:
        conditions.append({"prefix": {"key": prefix}})
    body = {"size": 200, "query": {"bool": {"must": conditions}},
            "sort": [{"key": "asc"}, {"file_id": "asc"}], "track_total_hits": True}
    while True:
        data = request("POST", index_path(index) + "/_search", body)
        hits = data["hits"]["hits"]
        if not hits:
            break
        for hit in hits:
            yield hit["_source"]
        body["search_after"] = hits[-1]["sort"]


def lexical(index, query, prefix, limit):
    body = {"size": limit, "query": {"bool": {
        "must": [{"multi_match": {"query": query, "fields": ["name^3", "body"]}}],
        "filter": [{"prefix": {"key": prefix}}] if prefix else []}},
        "highlight": {"fields": {"body": {"fragment_size": 400, "number_of_fragments": 3}},
                      "pre_tags": [""], "post_tags": [""]}}
    return request("POST", index_path(index) + "/_search", body)["hits"]["hits"]


def make_document(prepared, source_info, options):
    from app.rag import oss
    from app.rag.ingest import _strategy_version
    fingerprint = hashlib.sha256(json.dumps({"strategy": _strategy_version(), "options": options.as_dict()},
        sort_keys=True).encode()).hexdigest()
    units = prepared.text_units
    text = "\n\n".join(unit["text"] for unit in units)
    snapshot = {"body": text, "units": units, "content_sha256": prepared.content_sha256,
                "extraction_fingerprint": fingerprint}
    # Include the result hash: forced OCR refresh can yield different output for the same input/config.
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
    result_hash = hashlib.sha256(raw).hexdigest()
    key = f"{prepared.content_sha256}/{fingerprint}/{result_hash}.json"
    oss.OssStore(get_settings().oss_text_prefix).put_object(key, raw, content_type="application/json")
    return {"file_id": hashlib.sha256(prepared.key.encode()).hexdigest(), "key": prepared.key,
        "name": prepared.key.rsplit("/", 1)[-1], "body": text, "body_literal": text,
        "name_literal": prepared.key.rsplit("/", 1)[-1], "key_literal": prepared.key,
        "content_sha256": prepared.content_sha256, "snapshot_key": key, "units": units,
        "source_size": source_info.get("size", 0), "source_modified": source_info.get("last_modified")}
