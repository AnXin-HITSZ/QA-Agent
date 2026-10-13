"""Query-dependent graph expansion priorities; no model calls or inferred facts."""
from app.memory.text import bm25_scores, tokenize

RECALL_VERSION = "graph-recall/4"
_STOP = frozenset("a an the and or is are was were be been do does did what which who when where why how to of in on for from with it its his her their they he she have has had".split())


def priorities(data, query):
    """Score evidence, then propagate its maximum to supported structural nodes.

    Matching entity names are removed from the query terms so a high-degree
    person's name cannot dominate every fact attached to that person.
    """
    entity_terms = set()
    for entity in data["entities"]:
        if entity["name"].casefold() in query.casefold():
            entity_terms.update(tokenize(entity["name"]))
    terms = [t for t in tokenize(query) if t not in _STOP and t not in entity_terms]
    scores = {h.doc_id: h.score for h in bm25_scores(" ".join(terms),
              data.get("retrieval_texts", {}).items())}
    for link in data["links"]:
        fid = link["fact_id"]
        scores[fid] = max(scores.get(fid, 0), scores.get(link["memory_id"], 0))
    for relation in data["relations"]:
        score = scores.get(relation["id"], 0)
        for eid in (relation["subject_entity_id"], relation["object_entity_id"]):
            if eid:
                scores[eid] = max(scores.get(eid, 0), score)
    for participant in data["participants"]:
        eid = participant["entity_id"]
        if eid:
            scores[eid] = max(scores.get(eid, 0), scores.get(participant["event_id"], 0))
    return scores


def candidate_entities(data, memories, *, max_memories=3):
    """Only current authoritative candidate versions may seed confirmed bindings."""
    versions = {m["id"]: m for m in data["memories"]}
    valid = []
    for memory in list(memories)[:max(0, min(20, max_memories))]:
        expected = versions.get(memory.id)
        if (memory.user_id != data["user_id"] or memory.scope != data["scope"]
                or memory.status != "active" or not expected):
            continue
        if all(getattr(memory, key) == expected[key] for key in
               ("revision", "meta_version", "generation", "content_hash")):
            valid.append(memory.id)
    entities = {e["id"] for e in data["entities"]}
    out = []
    for mid in valid:
        found = [m["entity_id"] for m in data.get("mentions", []) if m["memory_id"] == mid]
        facts = {l["fact_id"] for l in data["links"] if l["memory_id"] == mid}
        for relation in data["relations"]:
            if relation["id"] in facts:
                found.extend([relation["subject_entity_id"], relation["object_entity_id"]])
        found.extend(p["entity_id"] for p in data["participants"] if p["event_id"] in facts)
        for eid in found:
            if eid in entities and eid not in out:
                out.append(eid)
    return out
