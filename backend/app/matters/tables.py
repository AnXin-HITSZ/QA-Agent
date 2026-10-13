from datetime import datetime
from sqlalchemy import String, Text, Integer, JSON, DateTime, Index, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.mysql import DATETIME

class Base(DeclarativeBase):
    pass

DT = DateTime().with_variant(DATETIME(fsp=6), 'mysql')

class Matter(Base):
    __tablename__ = 'admin_matters'
    __table_args__ = (Index('ix_admin_matters_owner', 'user_id', 'status', 'updated_at'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer)
    details: Mapped[dict] = mapped_column(JSON)
    deleted: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DT)
    updated_at: Mapped[datetime] = mapped_column(DT)

class Event(Base):
    __tablename__ = 'admin_matter_events'
    __table_args__ = (UniqueConstraint('user_id', 'request_id', name='uq_admin_event_request'), Index('ix_admin_events_matter', 'matter_id', 'created_at'))
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36))
    matter_id: Mapped[str] = mapped_column(String(32))
    request_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str] = mapped_column(String(500))
    before: Mapped[dict] = mapped_column('before_snapshot',JSON)
    after: Mapped[dict] = mapped_column('after_snapshot',JSON)
    created_at: Mapped[datetime] = mapped_column(DT)

class Link(Base):
    __tablename__ = 'admin_matter_links'
    __table_args__ = (Index('ix_admin_links_matter', 'matter_id', 'kind'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36))
    matter_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    details: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer)
    deleted: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DT)

class Projection(Base):
    __tablename__ = 'admin_matter_projections'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer)
    published_revision: Mapped[int] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer)
    last_error: Mapped[str] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(DT)


class Proposal(Base):
    __tablename__ = 'admin_matter_proposals'
    __table_args__ = (Index('ix_admin_proposals_thread', 'user_id', 'thread_id', 'status'),
        UniqueConstraint('user_id', 'dedupe_key', name='uq_admin_proposal_dedupe'))
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36))
    thread_id: Mapped[str] = mapped_column(String(160))
    matter_id: Mapped[str] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    before: Mapped[dict] = mapped_column('before_snapshot', JSON)
    dedupe_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DT)
    expires_at: Mapped[datetime] = mapped_column(DT)
