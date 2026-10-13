"""Exercise the real driver's transaction contract without a live server."""
from neo4j import Query
from app.config import Settings
from app.memory.graph.neo4j_client import Neo4jGraphClient


def test_publish_uses_strings_and_transaction_timeout(monkeypatch):
    calls = []
    class Result:
        def consume(self):
            return None
    class Transaction:
        def run(self, query, **params):
            assert isinstance(query, str), "Query objects are invalid in tx.run"
            calls.append((query, params))
            return Result()
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def run(self, query):
            assert isinstance(query, Query)
            assert query.timeout == 7
            return Result()
        def execute_write(self, work):
            assert work.timeout == 7
            work(Transaction())
    class Driver:
        def session(self, **kwargs):
            return Session()
    c = Neo4jGraphClient(Settings(_env_file=None, memory_graph_enabled=True,
        memory_graph_query_timeout_seconds=7))
    c._driver = Driver()
    monkeypatch.setattr(c, "connect", lambda: True)
    data = {"user_id": "test", "scope": "eval:offline", "generation": 0,
        "digest": "digest", "entities": [{"id": "e", "name": "Alpha"}],
        "relations": [], "events": [], "memories": [], "participants": [], "links": []}
    c.publish(data)
    assert len(calls) == 3  # delete owner, insert entity, write manifest
    data["entities"] = []
    c.publish(data)
    assert len(calls) == 4  # empty publication still clears the owner atomically


def test_recall_batches_frontiers_and_does_not_stop_at_first_memory(monkeypatch):
    settings = Settings(_env_file=None, memory_graph_max_paths=1,
                        memory_graph_max_nodes=8, memory_graph_fanout_limit=3)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    batches = []
    owner = "u:eval:test"
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def run(self, query, **params):
            if "keys" not in params:
                return [{"digest": "digest"}]
            batches.append(params["keys"])
            rows = []
            for key in params["keys"]:
                eid = key.rsplit(":", 1)[-1]
                neighbors = {"a": [("old", "memory"), ("event", "event")],
                             "b": [], "event": [("wanted", "memory")]}.get(eid, [])
                for nid, kind in neighbors:
                    rows.append(dict(source=key, key=f"{owner}:{kind}:{nid}", id=nid,
                                     kind=kind, role="evidence"))
            return rows
    class Driver:
        def session(self, **kw):
            return Session()
    client = Neo4jGraphClient(settings)
    client._driver = Driver()
    data = dict(user_id="u", scope="eval:test", digest="digest",
        entities=[dict(id="a", name="Alex"), dict(id="b", name="Blair")], aliases=[],
        relations=[], participants=[], links=[dict(fact_id="event", memory_id="wanted")],
        memories=[dict(id="old"), dict(id="wanted")],
        retrieval_texts={"old": "Alex joined a club", "wanted": "Alex repairs telescopes"})
    result = client.recall(data, query="What telescopes does Alex repair with Blair?")
    assert len(batches) == 2 and len(batches[0]) == 2
    assert result["candidates"] == ["wanted"]
    assert result["budget_exhausted"]  # One lower-priority path was omitted.
