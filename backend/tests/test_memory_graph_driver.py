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
