"""Permanent indexing audit and version policies; no external calls inside transactions."""
from __future__ import annotations

import hashlib
from sqlalchemy import JSON, Integer, String, Index, select, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from app.metering.db import configured, session_scope
from app.rag.search_repo import namespace


def audit_data(value):
    from app.metering.redact import safe_text
    if isinstance(value, dict):
        return {k: safe_text(v, limit=500) if k in ("error", "reason", "message") and isinstance(v, str)
                else audit_data(v) for k, v in value.items()}
    if isinstance(value, list):
        return [audit_data(v) for v in value]
    return value


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "rag_index_jobs"
    __table_args__ = (Index("ix_rag_index_jobs_namespace_order", "namespace", "created_at", "id"),)
    namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(16), default="queued")
    data: Mapped[dict] = mapped_column(JSON)


class File(Base):
    __tablename__ = "rag_index_job_files"
    namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class Policy(Base):
    __tablename__ = "rag_index_version_files"
    namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(128), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


def write(job):
    with session_scope() as db:
        db.merge(Job(namespace=namespace(), id=job["job_id"], created_at=job.get("created_at") or "",
                     status=job.get("status") or "queued", data=audit_data(job)))


def get(job_id):
    with session_scope() as db:
        row = db.get(Job, (namespace(), job_id))
        return row.data if row else None


def list_page(offset=0, limit=20, status=None):
    conditions = [Job.namespace == namespace()]
    if status:
        conditions.append(Job.status == status)
    with session_scope() as db:
        total = db.scalar(select(func.count()).select_from(Job).where(*conditions))
        rows = db.scalars(select(Job).where(*conditions).order_by(Job.created_at.desc(), Job.id.desc())
                          .offset(offset).limit(limit)).all()
        return {"total": total, "offset": offset, "limit": limit, "items": [row.data for row in rows]}


def append(job_id, detail, ordinal=None):
    with session_scope() as db:
        if ordinal is None:
            last = db.scalar(select(func.max(File.ordinal)).where(File.namespace == namespace(), File.job_id == job_id))
            ordinal = 0 if last is None else last + 1
        db.merge(File(namespace=namespace(), job_id=job_id, ordinal=ordinal, data=audit_data(detail)))


def files(job_id, offset=0, limit=100):
    with session_scope() as db:
        conditions = [File.namespace == namespace(), File.job_id == job_id]
        total = db.scalar(select(func.count()).select_from(File).where(*conditions))
        rows = db.scalars(select(File).where(*conditions).order_by(File.ordinal).offset(offset).limit(limit)).all()
        return {"total": total, "offset": offset, "limit": limit, "items": [r.data for r in rows]}


def policies(version):
    with session_scope() as db:
        return {r.data["key"]: r.data for r in db.scalars(select(Policy).where(
            Policy.namespace == namespace(), Policy.version == version)).all()}


def save_policies(version, rows):
    with session_scope() as db:
        for row in rows:
            db.merge(Policy(namespace=namespace(), version=version,
                            key_hash=hashlib.sha256(row["key"].encode()).hexdigest(), data=row))


def import_legacy(directory):
    """Idempotently import retained JSON/JSONL; never overwrite newer SQL progress."""
    import json
    imported = 0
    for path in sorted(directory.glob("*.json")):
        job = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(job, dict) or not job.get("job_id"):
            continue
        if get(job["job_id"]) is not None:
            continue
        detail_path = path.with_suffix(".files.jsonl")
        details = [json.loads(line) for line in detail_path.read_text(encoding="utf-8").splitlines() if line.strip()] if detail_path.exists() else []
        with session_scope() as db:
            # Locking/merge-free insert: concurrent importers cannot overwrite live rows.
            if db.get(Job, (namespace(), job["job_id"])):
                continue
            db.add(Job(namespace=namespace(), id=job["job_id"], created_at=job.get("created_at") or "",
                       status=job.get("status") or "failed", data=audit_data(job)))
            for ordinal, detail in enumerate(details):
                db.add(File(namespace=namespace(), job_id=job["job_id"], ordinal=ordinal, data=audit_data(detail)))
        imported += 1
    return imported
