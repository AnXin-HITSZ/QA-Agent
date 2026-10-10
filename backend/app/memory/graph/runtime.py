"""Durable graph writes and authoritative graph snapshots.

Graph jobs share the memory queue, leases and generation fence. Neo4j is a
projection: every recall is checked against a fresh authoritative snapshot.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from sqlalchemy import select, delete, inspect

from app.config import get_settings
from app.memory import db, repo as memory_repo
from app.memory.graph import resolve_switches, get_graph_client
from app.memory.graph import repo, extract
from app.memory.graph.tables import (MemoryEntityRow, MemoryEntityAliasRow, MemoryEntityMentionRow,
    MemoryRelationRow, MemoryEventRow, MemoryEventParticipantRow, MemoryFactLinkRow)

JOB_GRAPH = "graph_extract"
OP_GRAPH = "graph_sync"
PROJECTION_VERSION = "memory-graph/1"


def switches():
    return resolve_switches(get_settings())


def installed(session):
    return inspect(session.connection()).has_table("memory_entities")


def enqueue(session, item, *, now, force=False):
    if not switches().write:
        return None
    from app.memory.tables import MemoryJobRow
    pending = session.scalars(select(MemoryJobRow).where(
        MemoryJobRow.user_id == item.user_id, MemoryJobRow.scope == item.scope,
        MemoryJobRow.kind == JOB_GRAPH, MemoryJobRow.status == "pending",
        MemoryJobRow.thread_id == item.thread_id, MemoryJobRow.generation == item.generation)
        .order_by(MemoryJobRow.created_at.desc()).limit(1)).first()
    if pending and len((pending.payload or {}).get("memory_ids", [])) < 32:
        pending.payload = {"memory_ids": list(dict.fromkeys(
            [*(pending.payload or {}).get("memory_ids", []), item.id]))}
        session.flush()
        return pending.id
    import uuid
    suffix = ":" + uuid.uuid4().hex if force else ""
    return memory_repo.enqueue_job(session, user_id=item.user_id, scope=item.scope,
        kind=JOB_GRAPH, dedupe_key=f"{extract.PROTOCOL_VERSION}:{item.id}:{item.revision}:{item.meta_version}{suffix}",
        thread_id=item.thread_id, payload={"memory_ids": [item.id]}, now=now,
        max_attempts=get_settings().memory_job_max_attempts)


def queue_sync(session, *, user_id, scope, generation, now):
    return memory_repo.enqueue_op(session, user_id=user_id, scope=scope,
        kind=OP_GRAPH, generation=generation, memory_id="scope", now=now,
        max_attempts=get_settings().memory_op_max_attempts)


def invalidate(session, item, *, now):
    # Installed graph data must be withdrawn even with graph switches disabled.
    if not installed(session):
        return
    if not repo.links_for_memory(session, item.id) and not repo.mentions_for_memory(session, item.id):
        return
    repo.unlink_memory(session, item.id)
    session.execute(delete(MemoryEntityMentionRow).where(
        MemoryEntityMentionRow.memory_id == item.id,
        MemoryEntityMentionRow.user_id == item.user_id,
        MemoryEntityMentionRow.scope == item.scope))
    for table, kind in ((MemoryRelationRow, "relation"), (MemoryEventRow, "event")):
        orphan_ids = session.scalars(select(table.id).where(table.user_id == item.user_id,
            table.scope == item.scope, ~select(MemoryFactLinkRow.id).where(
                MemoryFactLinkRow.fact_id == table.id, MemoryFactLinkRow.fact_kind == kind).exists())).all()
        if kind == "event" and orphan_ids:
            session.execute(delete(MemoryEventParticipantRow).where(
                MemoryEventParticipantRow.event_id.in_(orphan_ids)))
        if orphan_ids:
            session.execute(delete(table).where(table.id.in_(orphan_ids)))
    queue_sync(session, user_id=item.user_id, scope=item.scope,
               generation=item.generation, now=now)


def purge(session, *, user_id, scope, generation, now):
    if not installed(session):
        return
    from app.memory.tables import MemoryOpRow
    has_data = session.scalar(select(MemoryEntityRow.id).where(
        MemoryEntityRow.user_id == user_id, MemoryEntityRow.scope == scope).limit(1))
    has_data = has_data or session.scalar(select(MemoryEventRow.id).where(
        MemoryEventRow.user_id == user_id, MemoryEventRow.scope == scope).limit(1))
    has_cleanup = session.scalar(select(MemoryOpRow.id).where(
        MemoryOpRow.user_id == user_id, MemoryOpRow.scope == scope,
        MemoryOpRow.kind == OP_GRAPH).limit(1))
    repo.delete_graph_for_user(session, user_id, scope=scope)
    if has_data or has_cleanup:
        queue_sync(session, user_id=user_id, scope=scope, generation=generation, now=now)


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        # Year/month precision remains in JSON; do not invent January 1st.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def retry_input(session, job):
    """Recover legacy failed graph jobs without restoring raw dialogue payloads."""
    from sqlalchemy import select
    from app.memory.tables import MemoryItemRow
    memory_repo.assert_generation(session, job.user_id, job.generation,
                                  scope=job.scope, now=db.utc_naive())
    ids = list((job.payload or {}).get("memory_ids", []))
    if not ids:
        if not job.thread_id:
            raise ValueError("图任务缺少可恢复的会话来源")
        ids = list(session.scalars(select(MemoryItemRow.id).where(
            MemoryItemRow.user_id == job.user_id, MemoryItemRow.scope == job.scope,
            MemoryItemRow.generation == job.generation, MemoryItemRow.status == "active",
            MemoryItemRow.thread_id == job.thread_id).order_by(MemoryItemRow.id).limit(501)))
        if not ids or len(ids) > 500:
            raise ValueError("图任务来源缺失或超过恢复上限，请分页重新登记图构建")
    ids = [f.id for f in memory_repo.list_by_ids(session, ids)
           if (f.user_id, f.scope, f.generation, f.status) ==
              (job.user_id, job.scope, job.generation, "active")]
    if not ids:
        raise ValueError("图任务来源缺失，不能登记空输入重试")
    return {"memory_ids": ids}


def apply(session, *, user_id, scope, facts, report, turn_key, generation):
    now = db.utc_naive()
    memory_repo.assert_generation(session, user_id, generation, scope=scope, now=now)
    current = {x.id: x for x in memory_repo.list_by_ids(session, [x.id for x in facts])}
    for fact in facts:
        row = current.get(fact.id)
        if not row or (row.user_id, row.scope, row.revision, row.meta_version, row.generation) != (
            user_id, scope, fact.revision, fact.meta_version, generation):
            raise ValueError("图提取期间事实版本已变化")
    identities = {}
    for entity in report.entities:
        evidence_facts = [facts[i] for i in entity.facts]
        candidates = repo.find_entities_by_name(session, user_id=user_id, scope=scope, name=entity.name)
        candidates = list({c.id: c for c in [*candidates,
            *repo.find_entities_by_alias(session, user_id=user_id, scope=scope, alias=entity.name)]}.values())
        # Name alone NEVER proves identity. Reuse a resolved mention tied to
        # the cited source memory; unresolved identity remains pending.
        known = {m.entity_id for f in evidence_facts for m in repo.mentions_for_memory(session, f.id)
                 if m.status == "resolved" and repo.name_key(m.surface) == repo.name_key(entity.name)}
        proven = [c for c in candidates if c.id in known]
        proven = [c for c in proven if c.generation == generation
                  and (c.kind == entity.kind or "unknown" in (c.kind, entity.kind))]
        # Extending an existing identity to another fact needs corroboration,
        # not merely a same-name old fact included in the model's input.
        linked = {f.id for f in evidence_facts if any(
            m.status == "resolved" and m.entity_id in known
            and repo.name_key(m.surface) == repo.name_key(entity.name)
            for m in repo.mentions_for_memory(session, f.id))}
        if linked and len(linked) < len(evidence_facts):
            from app.memory.graph.extract import _flatten, _grounded
            quote = entity.identity_quote
            distinctive = _flatten(quote).replace(_flatten(entity.name), "").strip()
            if len(distinctive) < 8 or not all(_grounded(quote, [f.text]) for f in evidence_facts):
                proven = []
        # Two same-name refs in one report cannot reuse the same ID.
        proven = [c for c in proven if c.id not in identities.values()]
        if len(proven) == 1:
            target = proven[0]
        elif any(c.id not in identities.values() for c in candidates):
            for f in evidence_facts:
                repo.add_mention(session, user_id=user_id, scope=scope, surface=entity.name,
                    memory_id=f.id, turn_key=turn_key + ":" + entity.ref, speaker=None, status="pending",
                    entity_id=None, evidence={"candidates": [c.id for c in candidates]},
                    generation=generation, now=now)
            continue
        else:
            target = repo.create_entity(session, user_id=user_id, scope=scope,
                name=entity.name, kind=entity.kind, generation=generation, now=now)
        identities[entity.ref] = target.id
        for alias in entity.aliases:
            repo.add_alias(session, entity_id=target.id, user_id=user_id, scope=scope,
                           alias=alias, origin="llm", now=now)
        for f in evidence_facts:
            repo.add_mention(session, user_id=user_id, scope=scope, surface=entity.name,
                memory_id=f.id, turn_key=turn_key + ":" + entity.ref, speaker=None, entity_id=target.id,
                status="resolved", evidence={"source": f.fact_context}, generation=generation, now=now)
            # A later evidence-backed decision also resolves earlier pending
            # mentions of this fact; confirmed identities are never overwritten.
            for mention in repo.mentions_for_memory(session, f.id):
                if (mention.status == "pending" and mention.generation == generation
                        and repo.name_key(mention.surface) == repo.name_key(entity.name)):
                    repo.resolve_mention(session, mention_id=mention.id, entity_id=target.id,
                        status="resolved", evidence={"decision": "corroborated_fact",
                                                     "identity_quote": entity.identity_quote})

    def link(kind, fid, indices, quote=""):
        for i in indices:
            f = facts[i]
            repo.link_fact(session, fact_kind=kind, fact_id=fid, memory_id=f.id,
                memory_revision=f.revision, user_id=user_id, scope=scope, turn_key=turn_key,
                generation=generation, now=now, quote=quote or f.text,
                meta={"meta_version": f.meta_version, "sources": (f.fact_context or {}).get("sources", [])})

    for rel in report.relations:
        subject = identities.get(rel.subject)
        obj = identities.get(rel.object_entity) if rel.object_entity else None
        if not subject or (rel.object_entity and not obj):
            continue
        f = facts[rel.facts[0]]
        existing = repo.find_relations(session, user_id=user_id, scope=scope,
            subject_entity_id=subject, relation_type=rel.relation,
            object_entity_id=obj, object_text=rel.object_text)
        compatible = [x for x in existing if x.generation == generation
                      and x.state == (f.fact_context or {}).get("state", "unknown")
                      and x.event_time == f.event_time]
        row = compatible[0] if compatible else repo.create_relation(session,
            user_id=user_id, scope=scope, subject_entity_id=subject, relation_type=rel.relation,
            object_entity_id=obj, object_text=rel.object_text, memory_id=f.id,
            generation=generation, now=now, event_time=f.event_time,
            state=(f.fact_context or {}).get("state", "unknown"))
        link("relation", row.id, rel.facts, rel.quote)
    for event in report.events:
        f = facts[event.description_fact]
        time = (f.fact_context or {}).get("time", {})
        row = repo.find_duplicate_event(session, memory_id=f.id, description=f.text)
        if row is None:
            row = repo.create_event(session, user_id=user_id, scope=scope, kind=event.kind,
                description=f.text, memory_id=f.id, generation=generation, now=now,
                time_expression=time.get("raw", ""), anchor=_parse_date(time.get("anchor")),
                start_at=_parse_date(time.get("start")), end_at=_parse_date(time.get("end")),
                time_precision=time.get("precision", "unknown"),
                status=(f.fact_context or {}).get("state", "unknown"),
                attributes={**event.attributes, "_time": time})
        for participant in event.participants:
            repo.add_participant(session, event_id=row.id, user_id=user_id, scope=scope,
                role=participant.role, name_text=participant.name,
                entity_id=identities.get(participant.entity_ref), now=now)
        link("event", row.id, event.facts)
    queue_sync(session, user_id=user_id, scope=scope, generation=generation, now=now)


def snapshot(session, *, user_id, scope):
    generation = memory_repo.generation_of(session, user_id, scope=scope)
    items = []
    after = ""
    while True:
        batch = memory_repo.index_items_page(session, scope=scope, user_id=user_id,
                                              after=after, limit=500)
        if not batch:
            break
        items.extend(batch)
        after = batch[-1].id
        if len(items) > 10000:
            raise ValueError("图快照超过安全上限，需分区重建")
    by_id = {f.id: f for f in items}
    links = session.scalars(select(MemoryFactLinkRow).where(
        MemoryFactLinkRow.user_id == user_id, MemoryFactLinkRow.scope == scope)).all()
    valid = [l for l in links if l.memory_id in by_id and l.generation == generation
             and l.memory_revision == by_id[l.memory_id].revision
             and int((l.meta or {}).get("meta_version", 0)) == by_id[l.memory_id].meta_version]
    relations = session.scalars(select(MemoryRelationRow).where(
        MemoryRelationRow.user_id == user_id, MemoryRelationRow.scope == scope,
        MemoryRelationRow.generation == generation, MemoryRelationRow.status == "active")).all()
    events = session.scalars(select(MemoryEventRow).where(
        MemoryEventRow.user_id == user_id, MemoryEventRow.scope == scope,
        MemoryEventRow.generation == generation)).all()
    fact_ids = {l.fact_id for l in valid}
    relations = [r for r in relations if r.id in fact_ids]
    events = [e for e in events if e.id in fact_ids]
    participants = session.scalars(select(MemoryEventParticipantRow).where(
        MemoryEventParticipantRow.user_id == user_id, MemoryEventParticipantRow.scope == scope)).all()
    mentions = session.scalars(select(MemoryEntityMentionRow).where(
        MemoryEntityMentionRow.user_id == user_id, MemoryEntityMentionRow.scope == scope,
        MemoryEntityMentionRow.status == "resolved", MemoryEntityMentionRow.generation == generation)).all()
    mentions = [m for m in mentions if m.memory_id in by_id]
    entity_ids = {r.subject_entity_id for r in relations} | {r.object_entity_id for r in relations}
    entity_ids |= {m.entity_id for m in mentions}
    entity_ids |= {p.entity_id for p in participants if p.event_id in fact_ids}
    entities = session.scalars(select(MemoryEntityRow).where(
        MemoryEntityRow.user_id == user_id, MemoryEntityRow.scope == scope,
        MemoryEntityRow.generation == generation, MemoryEntityRow.status == "active")).all()
    entities = [e for e in entities if e.id in entity_ids]
    aliases = session.scalars(select(MemoryEntityAliasRow).where(
        MemoryEntityAliasRow.user_id == user_id, MemoryEntityAliasRow.scope == scope)).all()
    used_memories = {l.memory_id for l in valid} | {m.memory_id for m in mentions}
    def serialize(row):
        return {c.name: (getattr(row, c.name).isoformat() if isinstance(getattr(row, c.name), datetime)
                        else getattr(row, c.name)) for c in row.__table__.columns}
    out = {"user_id": user_id, "scope": scope, "generation": generation,
           "protocol": PROJECTION_VERSION,
           "entities": [serialize(e) for e in entities],
           "relations": [serialize(r) for r in relations],
           "events": [serialize(e) for e in events],
           "participants": [serialize(p) for p in participants if p.event_id in fact_ids],
           "links": [serialize(l) for l in valid if l.fact_id in fact_ids],
           "mentions": [serialize(m) for m in mentions],
           "aliases": [serialize(a) for a in aliases if a.entity_id in entity_ids],
           "memories": [{"id": f.id, "revision": f.revision, "meta_version": f.meta_version,
                         "generation": f.generation, "content_hash": f.content_hash}
                        for f in items if f.id in used_memories]}
    out["digest"] = hashlib.sha256(json.dumps(out, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return out


def sync(*, user_id, scope):
    if not switches().master:
        raise RuntimeError("图组件关闭：清理任务保留待恢复")
    client = get_graph_client()
    if not client.connect():
        raise RuntimeError("Neo4j 不可用")
    # Serialize projection transactions with writes/purge for this owner. A late
    # worker cannot publish an old snapshot after a newer generation committed.
    with db.session_scope() as session:
        memory_repo.lock_user_state(session, user_id, scope=scope, now=db.utc_naive())
        data = snapshot(session, user_id=user_id, scope=scope)
        client.publish(data)
    return data["digest"]


def recall(*, user_id, scope, query):
    if not switches().search:
        return {"enabled": False, "candidates": [], "paths": []}
    client = get_graph_client()
    if not client.connect():
        raise RuntimeError("Neo4j 不可用")
    with db.session_scope() as session:
        data = snapshot(session, user_id=user_id, scope=scope)
    result = client.recall(data, query=query)
    result["versions"] = {m["id"]: m for m in data["memories"]}
    return result
