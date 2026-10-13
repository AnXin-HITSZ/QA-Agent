"""存量通用图的权威快照和可恢复撤回。没有抽取、维护或问答召回入口。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from sqlalchemy import select, delete, inspect

from app.config import get_settings
from app.memory import db, repo as memory_repo
from app.memory.graph import resolve_switches, get_graph_client
from app.memory.graph import repo
from app.memory.graph.tables import (MemoryEntityRow, MemoryEntityAliasRow, MemoryEntityMentionRow,
    MemoryRelationRow, MemoryEventRow, MemoryEventParticipantRow, MemoryFactLinkRow)

JOB_GRAPH = "graph_extract"
OP_GRAPH = "graph_sync"
PROJECTION_VERSION = "memory-graph/1"


def switches():
    return resolve_switches(get_settings())


def installed(session):
    return inspect(session.connection()).has_table("memory_entities")


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


def snapshot(session, *, user_id, scope, include_texts=False):
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
        if len(batch) < 500:
            break
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
    if include_texts:
        # Reuse the authoritative rows already read in this transaction. Text
        # stays outside the projection/digest and must never trigger a rebuild.
        out["retrieval_texts"] = {f.id: f.text for f in items if f.id in used_memories}
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
