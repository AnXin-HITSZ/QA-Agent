"""Optional real Neo4j integration; isolated random owner, no model calls."""
import os
import uuid
import pytest
from app.config import Settings
from app.memory.graph.neo4j_client import Neo4jGraphClient


@pytest.mark.skipif(not os.getenv("MEMORY_TEST_NEO4J_URI"), reason="dedicated Neo4j test endpoint not configured")
def test_real_publish_recall_drift_and_clear():
    uid = str(uuid.uuid4())
    c = Neo4jGraphClient(Settings(_env_file=None, memory_graph_enabled=True,
        neo4j_uri=os.environ["MEMORY_TEST_NEO4J_URI"],
        neo4j_password=os.environ["MEMORY_TEST_NEO4J_PASSWORD"],
        neo4j_username=os.getenv("MEMORY_TEST_NEO4J_USERNAME", "neo4j")))
    assert c.connect()
    data = {"user_id": uid, "scope": "eval:integration", "generation": 0, "digest": "test-digest",
            "entities": [{"id": "entity", "name": "EntityAlpha", "revision": 1}],
            "relations": [], "events": [], "participants": [], "links": [], "aliases": [],
            "mentions": [{"entity_id": "entity", "memory_id": "memory"}],
            "memories": [{"id": "memory", "revision": 1, "meta_version": 0, "content_hash": "hash", "generation": 0}]}
    try:
        c.publish(data)
        c.publish(data)
        assert c.audit(data)["clean"]
        assert c.recall(data, query="What did EntityAlpha do?")["candidates"] == ["memory"]
        with c._driver.session(database=c._database) as session:
            session.run("MATCH (n:QAMemoryGraph {key:$key}) SET n.revision=2",
                        key=uid + ":eval:integration:memory:memory").consume()
        assert not c.audit(data)["clean"]
        c.publish(data)
        assert c.audit(data)["clean"]
    finally:
        from neo4j import Query
        with c._driver.session(database=c._database) as session:
            session.run(Query("MATCH (n:QAMemoryGraph {owner:$owner}) DETACH DELETE n", timeout=15),
                        owner=uid + ":eval:integration").consume()
        c.close()
