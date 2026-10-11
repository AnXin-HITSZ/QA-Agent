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
