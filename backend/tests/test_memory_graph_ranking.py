from app.memory.graph.ranking import priorities


def test_question_attribute_beats_common_person_name():
    data = {"entities": [{"id": "person", "name": "Alex"}],
        "retrieval_texts": {"old": "Alex joined a club", "wanted": "Alex repairs telescopes"},
        "links": [{"fact_id": "event", "memory_id": "wanted"}],
        "relations": [], "participants": [{"entity_id": "person", "event_id": "event"}]}
    scores = priorities(data, "What telescopes does Alex repair?")
    assert scores.get("wanted", 0) > scores.get("old", 0)
    assert scores["event"] == scores["wanted"] == scores["person"]


def test_no_text_does_not_invent_relevance():
    assert priorities({"entities": [], "links": [], "relations": [],
                       "participants": [{"entity_id": None, "event_id": "event"}]},
                      "unknown") == {}


def test_candidate_seeds_require_current_owner_and_version():
    from types import SimpleNamespace
    from app.memory.graph.ranking import candidate_entities
    version = dict(id="m", revision=1, meta_version=0, generation=0, content_hash="h")
    data = dict(user_id="u", scope="s", memories=[version], entities=[{"id": "e"}],
                mentions=[{"memory_id": "m", "entity_id": "e"},
                          {"memory_id": "m", "entity_id": "missing"}],
                links=[], relations=[], participants=[])
    item = SimpleNamespace(**version, user_id="u", scope="s", status="active")
    assert candidate_entities(data, [item]) == ["e"]
    for key, value in [("scope", "other"), ("user_id", "other"), ("revision", 2),
                       ("generation", 1), ("meta_version", 1), ("content_hash", "old"),
                       ("status", "deleted")]:
        bad = SimpleNamespace(**vars(item))
        setattr(bad, key, value)
        assert candidate_entities(data, [bad]) == []


def test_candidate_seeds_include_relation_and_event_bindings_and_cap_memories():
    from types import SimpleNamespace
    from app.memory.graph.ranking import candidate_entities
    versions = [dict(id=str(i), revision=1, meta_version=0, generation=0, content_hash="h")
                for i in range(4)]
    data = dict(user_id="u", scope="s", memories=versions,
        entities=[{"id": e} for e in ["a", "b", "c", "d"]],
        mentions=[{"memory_id": "3", "entity_id": "d"}],
        links=[{"memory_id": "0", "fact_id": "r"}, {"memory_id": "1", "fact_id": "event"}],
        relations=[{"id": "r", "subject_entity_id": "a", "object_entity_id": "b"}],
        participants=[{"event_id": "event", "entity_id": "c"}])
    items = [SimpleNamespace(**v, user_id="u", scope="s", status="active") for v in versions]
    assert candidate_entities(data, items) == ["a", "b", "c"]


def test_seed_budget_configuration_is_bounded():
    import pytest
    from pydantic import ValidationError
    from app.config import Settings
    assert Settings(_env_file=None).memory_graph_seed_memory_k == 3
    assert Settings(_env_file=None, memory_graph_seed_memory_k=0).memory_graph_seed_memory_k == 0
    for invalid in (-1, 21):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, memory_graph_seed_memory_k=invalid)
