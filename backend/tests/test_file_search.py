"""Search contract, persistence/ownership and publication failure tests. No paid calls."""
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.rag import file_search as service, fulltext, search_repo as repo
from app.schemas.file_search import FileSearchRequest


@pytest.fixture
def database(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "search.db"))
    repo.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    @contextmanager
    def scope():
        with factory.begin() as db:
            yield db
    monkeypatch.setattr(repo, "session_scope", scope)
    yield scope
    engine.dispose()


def doc(key, body="聚餐报销", size=10):
    return {"key": key, "name": key.rsplit("/", 1)[-1], "body": body,
        "content_sha256": "hash", "source_size": size, "source_modified": 1,
        "snapshot_key": "snapshot", "units": [{"text": body, "page": 2, "section": ["说明"]}]}


def test_matching_fields_do_not_turn_directory_name_into_body_evidence():
    source = doc("王鸿鹏论文/english.pdf", body="Hongpeng Wang")
    default = service.item(source, ["vector"], ["王鸿鹏"])
    assert default["matched_terms"] == []
    assert default["matched_fields"] == {"name": [], "body": []}
    explicit = service.item(source, ["literal"], ["王鸿鹏"], fields=["path"])
    assert explicit["matched_fields"] == {"path": ["王鸿鹏"]}


def test_vector_snippet_can_prove_a_body_match_but_never_a_name_match():
    source = {"key": "papers/english.pdf"}
    row = service.item(source, ["vector"], ["王鸿鹏"], [{"text": "作者王鸿鹏"}])
    assert row["matched_fields"] == {"name": [], "body": ["王鸿鹏"]}


class Source:
    def __init__(self, docs):
        self.inventory = {d["key"]: {"key": d["key"], "size": d["source_size"], "last_modified": d["source_modified"]} for d in docs}
    def list_all(self, prefix=""):
        return [d for k, d in self.inventory.items() if k.startswith(prefix)]
    def stat(self, key):
        return self.inventory.get(key)


@pytest.fixture
def search_setup(monkeypatch, database):
    settings = get_settings().model_copy(update={"file_search_enabled": True})
    monkeypatch.setattr(fulltext, "get_settings", lambda: settings)
    docs = [doc(f"dir/{i:03}.md") for i in range(65)]
    source = Source(docs)
    monkeypatch.setattr(service.oss, "knowledge_store", lambda: source)
    monkeypatch.setattr(repo, "current", lambda: {"id": "version", "es_index": "index", "files": docs})
    monkeypatch.setattr(fulltext, "scan", lambda *a: iter(docs))
    return docs, source


def test_literal_all_matches_survive_pagination(search_setup):
    result = service.search(FileSearchRequest(literal_terms=["聚餐"], page_size=20), "alice")
    assert result["literal_total"] == 65 and result["supplement_total"] == 0
    keys = []
    while True:
        keys.extend(r["key"] for r in result["items"])
        if result["next_offset"] is None:
            break
        result = service.page(result["search_id"], "alice", result["next_offset"])
    assert len(keys) == len(set(keys)) == 65


def test_literal_set_is_protected_from_supplement_ranking(monkeypatch, search_setup):
    observed = []
    def supplements(req, release, inventory, protected):
        observed.append(protected)
        return [service.item(doc("extra.md"), ["vector"], [])], []
    source = search_setup[1]
    source.inventory["extra.md"] = {"key": "extra.md", "size": 10, "last_modified": 1}
    monkeypatch.setattr(service, "supplements", supplements)
    result = service.search(FileSearchRequest(mode="semantic", query="聚餐", literal_terms=["聚餐"]), "alice")
    assert result["total"] == 66 and result["literal_total"] == 65
    assert len(observed[0]) == 65
    final = service.page(result["search_id"], "alice", 60)
    assert final["items"][-1]["routes"] == ["vector"]


def test_search_results_cannot_be_read_by_another_user(search_setup):
    result = service.search(FileSearchRequest(literal_terms=["聚餐"]), "alice")
    with pytest.raises(LookupError):
        service.page(result["search_id"], "bob")


def test_deleted_and_changed_files_do_not_expose_saved_evidence(search_setup):
    result = service.search(FileSearchRequest(literal_terms=["聚餐"]), "alice")
    source = search_setup[1]
    source.inventory.pop("dir/000.md")
    source.inventory["dir/001.md"]["last_modified"] = 2
    page = service.page(result["search_id"], "alice")
    assert page["unavailable_on_page"] == 2 and len(page["items"]) == 18
    assert page["next_offset"] == 20


def test_expired_session_is_inaccessible(database):
    sid = repo.new_session("alice", "release", {"total": 0})
    repo.finish(sid, {"total": 0})
    with database() as db:
        db.get(repo.SearchSession, sid).expires_at = repo.utc_naive() - timedelta(seconds=1)
    with pytest.raises(LookupError):
        repo.read_page(sid, "alice")


def test_unindexed_filename_can_match_and_body_is_reported_uncovered(monkeypatch, search_setup):
    docs, source = search_setup
    source.inventory["聚餐新规.pdf"] = {"key": "聚餐新规.pdf", "size": 20, "last_modified": 2}
    result = service.search(FileSearchRequest(literal_terms=["聚餐"]), "alice")
    assert result["total"] == 66 and result["coverage"]["body_uncovered"] == 1
    row = service.page(result["search_id"], "alice", 60)["items"][-1]
    assert row["body_indexed"] is False and row["evidence"] == []


def test_substring_and_boolean_contract():
    assert service.literal_matches(["报销申请须附发票"], ["发票", "报销"], "all")
    assert not service.literal_matches(["报销申请須附发票"], ["发票报销"], "all")
    assert service.literal_matches(["只有报销"], ["发票", "报销"], "any")
    assert not service.literal_matches(["只有报销"], ["发票", "报销"], "all")


def test_literal_query_escapes_wildcards_and_does_not_trim_input():
    query = fulltext.literal_query(["a*?\\"], "all", ["body"])
    pattern = query["bool"]["must"][0]["bool"]["should"][0]["wildcard"]["body_literal"]["value"]
    assert pattern == "*a\\*\\?\\\\*"
    assert FileSearchRequest(literal_terms=[" 聚餐 "]).literal_terms == [" 聚餐 "]


def test_release_compare_and_swap_preserves_newer_release(database):
    repo.prepare_release("old", "es-old", [])
    repo.activate("old", None)
    repo.prepare_release("new", "es-new", [])
    repo.activate("new", "old")
    repo.prepare_release("stale", "es-stale", [])
    with pytest.raises(RuntimeError, match="其他任务"):
        repo.activate("stale", "old")
    assert repo.current()["id"] == "new"
    repo.rollback()
    assert repo.current()["id"] == "old"


def test_partial_shard_response_is_not_called_complete(monkeypatch):
    settings = get_settings().model_copy(update={"file_search_enabled": True, "elasticsearch_url": "http://localhost:9200"})
    monkeypatch.setattr(fulltext, "get_settings", lambda: settings)
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"_shards": {"failed": 1}})
    monkeypatch.setattr(fulltext.httpx, "Client", Client)
    with pytest.raises(fulltext.SearchUnavailable, match="未完整"):
        fulltext.request("POST", "/index/_search", {})


def test_fulltext_and_vector_merge_reranks_only_non_literal(monkeypatch):
    docs = [doc("literal.md"), doc("shared.md"), doc("vector.md")]
    inventory = Source(docs).inventory
    release = {"id": "release", "es_index": "index", "files": docs}
    monkeypatch.setattr(fulltext, "lexical", lambda *a: [{"_source": docs[0]}, {"_source": docs[1]}])
    monkeypatch.setattr(service, "vector_candidates", lambda *a: [
        {**docs[0], "evidence": []}, {**docs[1], "evidence": [{"text": "凭证", "page": None, "section": []}]},
        {**docs[2], "evidence": [{"text": "票据", "page": None, "section": []}]}])
    from app.memory import rerank
    inputs = []
    monkeypatch.setattr(rerank, "configured", lambda: True)
    def rank(query, texts, top_n):
        inputs.extend(texts)
        return rerank.RerankOutcome(order=list(reversed(range(len(texts)))), scores={i: .9 for i in range(len(texts))})
    monkeypatch.setattr(rerank, "rerank", rank)
    result, notes = service.supplements(FileSearchRequest(mode="semantic", query="票据", literal_terms=["发票"]), release, inventory, {"literal.md"})
    assert len(result) == 2 and not any("literal.md" in text for text in inputs)
    assert set(next(r for r in result if r["key"] == "shared.md")["routes"]) == {"fulltext", "vector"}


def test_literal_failure_does_not_create_partial_result_set(monkeypatch, search_setup, database):
    def fail(*args):
        raise fulltext.SearchUnavailable("故障")
    monkeypatch.setattr(fulltext, "scan", fail)
    with pytest.raises(fulltext.SearchUnavailable):
        service.search(FileSearchRequest(literal_terms=["聚餐"]), "alice")
    from sqlalchemy import select
    with database() as db:
        assert list(db.scalars(select(repo.SearchSession))) == []


@pytest.fixture
def paired_index(monkeypatch, kb_env, database):
    monkeypatch.setattr(kb_env.settings, "file_search_enabled", True)
    indices = {}
    def create(collection):
        name = "es-" + collection
        indices[name] = {}
        return name
    monkeypatch.setattr(fulltext, "create_index", create)
    monkeypatch.setattr(fulltext, "put_document", lambda name, value: indices[name].update({value["key"]: value}))
    monkeypatch.setattr(fulltext, "scan", lambda name: iter(indices[name].values()))
    monkeypatch.setattr(fulltext, "refresh", lambda name: None)
    monkeypatch.setattr(fulltext, "remove_index", lambda name: indices.pop(name))
    monkeypatch.setattr(fulltext, "request", lambda *args: {})
    snapshots = {}
    class SnapshotStore:
        def __init__(self, prefix): pass
        def put_object(self, key, raw, **kwargs): snapshots[key] = raw
    monkeypatch.setattr(service.oss, "OssStore", SnapshotStore)
    kb_env.kb.files.update({"a.md": b"# Invoice\n\nReturn invoice for reimbursement.", "b.md": b"Other policy."})
    yield kb_env, indices, snapshots


def test_paired_publication_preserves_complete_text_and_rolls_back(paired_index):
    from app.rag import ingest, store
    kb, indices, snapshots = paired_index
    result = ingest.run_index_job(kb.kb, ingest.Scope(), ingest.Options())
    assert result["published"]
    first = repo.current()
    assert first["id"] == store.active_collection()
    assert len(first["files"]) == 2 and len(snapshots) == 2
    assert "Return invoice" in indices[first["es_index"]]["a.md"]["body"]
    kb.kb.files["a.md"] = b"Changed policy."
    kb.kb.touch("a.md")
    result = ingest.run_index_job(kb.kb, ingest.Scope(kind="keys", keys=("a.md",)), ingest.Options())
    assert result["published"] and repo.current()["id"] != first["id"]
    assert indices[repo.current()["es_index"]]["b.md"] == indices[first["es_index"]]["b.md"]
    store.rollback()
    assert repo.current()["id"] == store.active_collection() == first["id"]


def test_fulltext_write_failure_does_not_publish_or_remove_active(paired_index, monkeypatch):
    from app.rag import ingest, store
    kb, indices, _ = paired_index
    first = ingest.run_index_job(kb.kb, ingest.Scope(), ingest.Options())
    active = repo.current()
    def fail(*args): raise fulltext.SearchUnavailable("write failure")
    monkeypatch.setattr(fulltext, "put_document", fail)
    result = ingest.run_index_job(kb.kb, ingest.Scope(), ingest.Options())
    assert not result["published"]
    assert repo.current()["id"] == first["index_version"]
    assert active["es_index"] in indices and store.collection_exists(first["index_version"])


def test_committed_release_survives_legacy_manifest_mirror_failure(paired_index, monkeypatch):
    from app.rag import ingest, store
    kb, _, _ = paired_index
    original = store._write_manifest
    def fail_only_after_activation(data):
        if repo.current() is not None:
            raise OSError("mirror unavailable")
        return original(data)
    monkeypatch.setattr(store, "_write_manifest", fail_only_after_activation)
    result = ingest.run_index_job(kb.kb, ingest.Scope(), ingest.Options())
    assert result["published"] and store.collection_exists(repo.current()["id"])


def test_vector_file_grouping_uses_real_qdrant_contract(paired_index, monkeypatch):
    from app.rag import embeddings, ingest
    kb, _, _ = paired_index
    monkeypatch.setattr(embeddings, "get_embeddings", lambda: kb.embeddings)
    result = ingest.run_index_job(kb.kb, ingest.Scope(), ingest.Options())
    candidates = service.vector_candidates("invoice", ["reimbursement"], result["index_version"], "", 20, ["a.md", "b.md"])
    assert candidates and {row["key"] for row in candidates} <= {"a.md", "b.md"}
    assert all(len(row["evidence"]) <= 3 for row in candidates)
