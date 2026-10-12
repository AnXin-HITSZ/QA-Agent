"""Bounded original user evidence. Never stores tool/assistant output or writes inferred facts."""
from datetime import timedelta
import hashlib
import json
import uuid

from sqlalchemy import delete, inspect, select, update
from sqlalchemy.exc import IntegrityError
from app.config import get_settings
from app.memory import db, repo
from app.memory.tables import MemoryEpisodeRow, MemoryItemRow, MemorySourceRow, MemoryHistoryRow
from app.memory.extract import looks_secret

PROTOCOL = "source-evidence/1"


def fingerprint(user_ids, scope):
    digest = hashlib.sha256()
    with db.session_scope() as session:
        rows = session.execute(select(MemoryEpisodeRow.id, MemoryEpisodeRow.generation,
            MemoryEpisodeRow.content_hash, MemoryEpisodeRow.status, MemoryEpisodeRow.expires_at).where(
            MemoryEpisodeRow.user_id.in_(user_ids), MemoryEpisodeRow.scope == scope)
            .order_by(MemoryEpisodeRow.id).execution_options(yield_per=500))
        for row in rows:
            digest.update(json.dumps(list(row), default=str).encode())
    return digest.hexdigest()


def retain(session, *, user_id, scope, turn_id, messages, now, expected_generation=None):
    s = get_settings()
    if not s.memory_source_store_enabled:
        return False
    cleaned, used = [], 0
    for i, msg in enumerate(messages):
        content = str(msg.get("content") or "")
        if msg.get("role") != "user" or not content or looks_secret(content):
            continue
        if used + len(content) > max(1, s.memory_source_max_chars):
            break
        cleaned.append({"content": content, "speaker": str(msg.get("speaker") or "")[:160],
            "message_id": str(msg.get("message_id") or f"m{i}")[:160],
            "recorded_at": str(msg.get("recorded_at") or "")[:100]})
        used += len(content)
    if not cleaned:
        return False
    generation = repo.assert_generation(session, user_id, expected_generation, scope=scope, now=now)
    withdrawn = session.scalar(select(MemorySourceRow.id).join(MemoryHistoryRow,
        MemoryHistoryRow.memory_id == MemorySourceRow.memory_id).where(
        MemorySourceRow.user_id == user_id, MemorySourceRow.scope == scope, MemorySourceRow.turn_id == turn_id,
        MemoryHistoryRow.user_id == user_id, MemoryHistoryRow.scope == scope,
        MemoryHistoryRow.actor == "user", MemoryHistoryRow.event.in_(["UPDATE","DELETE"]),
        MemoryHistoryRow.created_at >= MemorySourceRow.created_at).limit(1))
    if withdrawn:
        return False  # retroactive import cannot restore a manually withdrawn source
    digest = hashlib.sha256(json.dumps(cleaned, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    try:
        with session.begin_nested():
            session.add(MemoryEpisodeRow(id=uuid.uuid4().hex, user_id=user_id, scope=scope,
                turn_id=turn_id, generation=generation, messages=cleaned, content_hash=digest,
                status="active", created_at=now,
                expires_at=now + timedelta(days=max(1, s.memory_source_retention_days))))
            session.flush()
    except IntegrityError:
        return False  # revoked or previously imported turns cannot be resurrected
    expired = list(session.scalars(select(MemoryEpisodeRow.id).where(
        MemoryEpisodeRow.user_id == user_id, MemoryEpisodeRow.scope == scope,
        MemoryEpisodeRow.status == "active", MemoryEpisodeRow.expires_at <= now).limit(100)))
    if expired:
        session.execute(update(MemoryEpisodeRow).where(MemoryEpisodeRow.id.in_(expired))
                        .values(status="expired",messages=[]))
    excess = list(session.scalars(select(MemoryEpisodeRow.id).where(
        MemoryEpisodeRow.user_id == user_id, MemoryEpisodeRow.scope == scope, MemoryEpisodeRow.status == "active")
        .order_by(MemoryEpisodeRow.created_at.desc(), MemoryEpisodeRow.id.desc())
        .offset(max(1, s.memory_source_max_episodes)).limit(100)))
    if excess:
        session.execute(update(MemoryEpisodeRow).where(MemoryEpisodeRow.id.in_(excess))
                        .values(status="evicted",messages=[]))
    return True


def revoke(session, *, user_id, scope, memory_id):
    if not inspect(session.get_bind()).has_table("memory_episodes"):
        return
    turns = select(MemorySourceRow.turn_id).where(MemorySourceRow.user_id == user_id,
        MemorySourceRow.scope == scope, MemorySourceRow.memory_id == memory_id)
    session.execute(update(MemoryEpisodeRow).where(MemoryEpisodeRow.user_id == user_id,
        MemoryEpisodeRow.scope == scope, MemoryEpisodeRow.turn_id.in_(turns))
        .values(status="revoked", messages=[]))


def purge(session, *, user_id, scope):
    if inspect(session.get_bind()).has_table("memory_episodes"):
        return int(session.execute(delete(MemoryEpisodeRow).where(MemoryEpisodeRow.user_id == user_id,
            MemoryEpisodeRow.scope == scope)).rowcount)
    return 0


def repair_source_links(session, *, user_id, scope, turn_id, now, expected_generation):
    """Backfill only backend-stamped origins, never inferred or user-withdrawn facts."""
    generation = repo.assert_generation(session, user_id, expected_generation, scope=scope, now=now)
    withdrawn = select(MemoryHistoryRow.memory_id).where(
        MemoryHistoryRow.user_id == user_id, MemoryHistoryRow.scope == scope,
        MemoryHistoryRow.actor == "user", MemoryHistoryRow.event.in_(["UPDATE", "DELETE"]))
    ids = session.scalars(select(MemoryItemRow.id).where(
        MemoryItemRow.user_id == user_id, MemoryItemRow.scope == scope,
        MemoryItemRow.status == "active", MemoryItemRow.generation == generation,
        MemoryItemRow.fact_context["turn_id"].as_string() == turn_id,
        MemoryItemRow.id.not_in(withdrawn))).all()
    return repo.add_sources(session, memory_ids=ids, user_id=user_id, scope=scope,
                            conversation_id=turn_id, turn_id=turn_id, now=now)


def corpus(user_id, scope):
    """Only current-generation episodes linked to at least one active owned fact."""
    s = get_settings()
    with db.session_scope() as session:
        generation = repo.generation_of(session, user_id, scope=scope)
        linked = select(MemorySourceRow.turn_id).join(MemoryItemRow,
            MemoryItemRow.id == MemorySourceRow.memory_id).where(
            MemorySourceRow.user_id == user_id, MemorySourceRow.scope == scope,
            MemoryItemRow.user_id == user_id, MemoryItemRow.scope == scope,
            MemoryItemRow.status == "active", MemoryItemRow.generation == generation)
        rows = session.scalars(select(MemoryEpisodeRow).where(
            MemoryEpisodeRow.user_id == user_id, MemoryEpisodeRow.scope == scope,
            MemoryEpisodeRow.generation == generation, MemoryEpisodeRow.status == "active",
            MemoryEpisodeRow.expires_at > db.utc_naive(), MemoryEpisodeRow.turn_id.in_(linked))
            .order_by(MemoryEpisodeRow.created_at.desc(), MemoryEpisodeRow.id.desc())
            .limit(max(1, s.memory_source_max_episodes))).all()
        out, used = [], 0
        for row in rows:
            for i, msg in enumerate(row.messages):
                # Adjacent utterances retain local reference context; always whole messages.
                group = row.messages[max(0, i-1):i+2]
                text = "\n".join(f"[{m['message_id']}｜{m['recorded_at']}] {m['speaker']}: {m['content']}" for m in group)
                used += len(text)
                if used > max(1, s.memory_source_max_chars):
                    return out, True
                out.append({"id": f"{row.id}:{i}", "episode_id": row.id, "turn_id": row.turn_id,
                    "speaker": msg.get("speaker", ""),
                    "message_ids": [m["message_id"] for m in group], "text": text,
                    "generation": generation, "content_hash": row.content_hash})
        return out, len(rows) >= max(1, s.memory_source_max_episodes)


def validate(user_id, scope, snippets):
    if not snippets:
        return []
    current, _ = corpus(user_id, scope)
    valid = {x["id"]: x for x in current}
    return [x for x in snippets if x["id"] in valid and
            valid[x["id"]]["content_hash"] == x["content_hash"] and
            valid[x["id"]]["generation"] == x["generation"]]
