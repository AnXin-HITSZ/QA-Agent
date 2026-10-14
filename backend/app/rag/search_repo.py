"""File-search metadata and owner-scoped result sets. DDL only via migration 0014.

No external calls inside SQL transactions. Text bodies live in OSS/Elasticsearch.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Index, delete, select
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings
from app.metering.db import session_scope, utc_naive


class Base(DeclarativeBase):
    pass


_DT6 = DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")


class Release(Base):
    __tablename__ = "rag_search_releases"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    es_index: Mapped[str] = mapped_column(String(128))
    files: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[object] = mapped_column(_DT6, default=utc_naive)


class State(Base):
    __tablename__ = "rag_search_state"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    active: Mapped[str | None] = mapped_column(String(128), nullable=True)
    previous: Mapped[str | None] = mapped_column(String(128), nullable=True)


class SearchSession(Base):
    __tablename__ = "rag_file_search_sessions"
    __table_args__ = (Index("ix_rag_search_user", "user_id"), Index("ix_rag_search_expiry", "expires_at"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64))
    release_id: Mapped[str] = mapped_column(String(128))
    metadata_json: Mapped[dict] = mapped_column(JSON)
    ready: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[object] = mapped_column(_DT6)


class SearchItem(Base):
    __tablename__ = "rag_file_search_items"
    session_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    item: Mapped[dict] = mapped_column(JSON)


def namespace():
    # Separates multiple logical knowledge bases sharing one application DB.
    import hashlib
    s = get_settings()
    return hashlib.sha256(f"{s.oss_bucket}|{s.oss_prefix}|{s.qdrant_collection}".encode()).hexdigest()


def current() -> dict | None:
    with session_scope() as db:
        state = db.get(State, namespace())
        row = db.get(Release, state.active) if state and state.active else None
        return {"id": row.id, "es_index": row.es_index, "files": row.files,
                "previous": state.previous} if row else None


def prepare_release(collection, es_index, files):
    with session_scope() as db:
        db.add(Release(id=collection, es_index=es_index, files=files))


def activate(collection, expected):
    with session_scope() as db:
        state = db.execute(select(State).where(State.id == namespace()).with_for_update()).scalar_one_or_none()
        if not db.get(Release, collection):
            raise RuntimeError("全文候选版本未准备，拒绝发布")
        if state is None:
            state = State(id=namespace(), active=None, previous=None)
            db.add(state)
        if state.active != expected:
            raise RuntimeError("索引版本已被其他任务更新，拒绝覆盖发布")
        state.previous, state.active = state.active, collection


def rollback(expected=None):
    with session_scope() as db:
        state = db.execute(select(State).where(State.id == namespace()).with_for_update()).scalar_one_or_none()
        if not state or not state.previous:
            raise RuntimeError("没有可回退的联合索引版本")
        if expected is not None and (state.active, state.previous) != expected:
            raise RuntimeError("版本已变化，请重新执行回退")
        row = db.get(Release, state.previous)
        if not row:
            raise RuntimeError("上一版本记录缺失")
        state.active, state.previous = state.previous, state.active
        return {"active": state.active, "previous": state.previous}


def rollback_target():
    with session_scope() as db:
        state = db.get(State, namespace())
        row = db.get(Release, state.previous) if state and state.previous else None
        if not row:
            raise RuntimeError("没有可回退的联合索引版本")
        return row.id, row.es_index, (state.active, state.previous)


def new_session(user_id, release_id, metadata):
    sid = uuid.uuid4().hex
    with session_scope() as db:
        expired = list(db.scalars(select(SearchSession.id).where(SearchSession.expires_at < utc_naive()).limit(100)))
        # Bounded opportunistic cleanup; remaining expired rows are never readable.
        for old in expired[:100]:
            db.execute(delete(SearchItem).where(SearchItem.session_id == old))
            db.execute(delete(SearchSession).where(SearchSession.id == old))
        db.add(SearchSession(id=sid, user_id=str(user_id), release_id=release_id,
            metadata_json=metadata, expires_at=utc_naive() + timedelta(seconds=get_settings().file_search_session_seconds)))
    return sid


def append_items(sid, offset, items):
    with session_scope() as db:
        db.add_all([SearchItem(session_id=sid, ordinal=offset + i, item=item) for i, item in enumerate(items)])


def finish(sid, metadata):
    with session_scope() as db:
        row = db.get(SearchSession, sid)
        row.metadata_json, row.ready = metadata, True


def remove_session(sid):
    with session_scope() as db:
        db.execute(delete(SearchItem).where(SearchItem.session_id == sid))
        db.execute(delete(SearchSession).where(SearchSession.id == sid))


def read_page(sid, user_id, offset=0, size=20):
    with session_scope() as db:
        row = db.get(SearchSession, sid)
        if not row or row.user_id != str(user_id) or not row.ready or row.expires_at <= utc_naive():
            raise LookupError("搜索结果不存在或已过期")
        items = list(db.scalars(select(SearchItem).where(SearchItem.session_id == sid,
            SearchItem.ordinal >= offset).order_by(SearchItem.ordinal).limit(size)))
        return dict(row.metadata_json), [dict(item.item) for item in items]
