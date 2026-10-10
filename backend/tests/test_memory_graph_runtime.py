"""Transactional graph integration tests: SQLite SQL + explicit service doubles."""
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.memory import repo, db
from app.memory.graph import runtime, extract
from app.memory.graph.tables import Base, MemoryEntityRow, MemoryRelationRow, MemoryFactLinkRow
from app.memory.tables import MemoryJobRow, MemoryOpRow
from app.memory.worker import MemoryWorker

USER = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def env(memory_db, monkeypatch):
    Base.metadata.create_all(memory_db.db.get_engine())
    monkeypatch.setattr(memory_db.settings, "memory_graph_enabled", True)
    monkeypatch.setattr(memory_db.settings, "memory_graph_write_enabled", True)
    monkeypatch.setattr(memory_db.settings, "memory_graph_worker_enabled", True)
    monkeypatch.setattr(memory_db.settings, "memory_write_enabled", True)
    class Environment:
        settings = memory_db.settings
    return Environment()


def add(text="甲负责乙项目", scope=""):
    with db.session_scope() as session:
        return repo.create_item(session, user_id=USER, scope=scope, text=text, now=db.utc_naive(), thread_id="t")


def report(facts):
    index = next(i for i, f in enumerate(facts) if f.text == "甲负责乙项目")
    return extract._parse(json.dumps({"entities": [{"ref": "a", "name": "甲", "kind": "person", "facts": [index]},
        {"ref": "b", "name": "乙项目", "kind": "topic", "facts": [index]}],
        "relations": [{"subject": "a", "relation": "负责", "object_entity": "b", "facts": [index],
                       "quote": "甲负责乙项目"}], "events": []}), facts=facts)


def test_transaction_rollback_does_not_leave_graph_job(env):
    with pytest.raises(RuntimeError), db.session_scope() as session:
        repo.create_item(session, user_id=USER, text="甲负责乙项目", now=db.utc_naive())
        raise RuntimeError("rollback")
    with db.session_scope() as session:
        assert not session.scalars(select(MemoryJobRow)).all()


def test_grouped_jobs_apply_and_delete_current_projection(env, monkeypatch):
    a = add()
    b = add("乙项目使用丙技术")
    with db.session_scope() as session:
        jobs = session.scalars(select(MemoryJobRow)).all()
        assert len(jobs) == 1
        assert set(jobs[0].payload["memory_ids"]) == {a.id, b.id}
    monkeypatch.setattr(extract, "extract_graph", lambda facts, messages: report(facts))
    worker = MemoryWorker()
    monkeypatch.setattr(worker, "_replay_ops", lambda **kw: 0)
    assert worker.run_once(limit=1, force=True)["succeeded"] == 1
    with db.session_scope() as session:
        data = runtime.snapshot(session, user_id=USER, scope="")
        assert len(data["relations"]) == 1 and len(data["entities"]) == 2
        repo.delete_item(session, user_id=USER, memory_id=a.id, actor="user", now=db.utc_naive())
    with db.session_scope() as session:
        data = runtime.snapshot(session, user_id=USER, scope="")
        assert not data["relations"]
        assert all(m["memory_id"] != a.id for m in data["mentions"])


def test_same_name_refs_are_not_collapsed(env):
    a = add("甲在乙项目工作；另一位甲在丙项目工作")
    r = extract._parse(json.dumps({"entities": [
        {"ref": "a", "name": "甲", "kind": "person", "facts": [0]},
        {"ref": "b", "name": "甲", "kind": "person", "facts": [0]}],
        "relations": [], "events": []}), facts=[a])
    with db.session_scope() as session:
        runtime.apply(session, user_id=USER, scope="", facts=[a], report=r, turn_key="turn", generation=a.generation)
        assert len(session.scalars(select(MemoryEntityRow)).all()) == 2


def test_shared_fact_survives_one_source_delete(env):
    a = add()
    b = add("甲负责乙项目的运维")
    r = report([a])
    with db.session_scope() as session:
        runtime.apply(session, user_id=USER, scope="", facts=[a], report=r, turn_key="one", generation=a.generation)
        rel = session.scalars(select(MemoryRelationRow)).one()
        from app.memory.graph import repo as graph_repo
        graph_repo.link_fact(session, fact_kind="relation", fact_id=rel.id, memory_id=b.id,
            memory_revision=b.revision, user_id=USER, scope="", turn_key="two", generation=b.generation,
            now=db.utc_naive(), meta={"meta_version": b.meta_version})
        repo.delete_item(session, user_id=USER, memory_id=a.id, actor="user", now=db.utc_naive())
        assert len(runtime.snapshot(session, user_id=USER, scope="")["relations"]) == 1


def test_purge_removes_sql_graph_and_preserves_cleanup_when_disabled(env, monkeypatch):
    a = add(scope="eval:test")
    with db.session_scope() as session:
        runtime.apply(session, user_id=USER, scope=a.scope, facts=[a], report=report([a]), turn_key="x", generation=a.generation)
    monkeypatch.setattr(env.settings, "memory_graph_enabled", False)
    with db.session_scope() as session:
        repo.purge_user(session, USER, scope=a.scope, now=db.utc_naive())
        assert not session.scalars(select(MemoryEntityRow)).all()
        assert session.scalar(select(MemoryOpRow.id).where(MemoryOpRow.kind == runtime.OP_GRAPH))
    with pytest.raises(RuntimeError):
        runtime.sync(user_id=USER, scope=a.scope)


def test_off_empty_graph_does_not_invent_cleanup(env, monkeypatch):
    monkeypatch.setattr(env.settings, "memory_graph_enabled", False)
    a = add()
    with db.session_scope() as session:
        repo.purge_user(session, USER, now=db.utc_naive())
        assert not session.scalars(select(MemoryOpRow)).all()


def test_failed_apply_reuses_paid_stage(env, monkeypatch):
    add()
    calls = []
    monkeypatch.setattr(extract, "extract_graph", lambda facts, messages: (calls.append(1) or report(facts)))
    original = runtime.apply
    monkeypatch.setattr(runtime, "apply", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("fail")))
    worker = MemoryWorker()
    monkeypatch.setattr(worker, "_replay_ops", lambda **kw: 0)
    assert worker.run_once(limit=1, force=True)["failed"] == 1
    with db.session_scope() as session:
        job = session.scalars(select(MemoryJobRow)).one()
        job.next_run_at = db.utc_naive()
    monkeypatch.setattr(runtime, "apply", original)
    assert worker.run_once(limit=1, force=True)["succeeded"] == 1
    assert len(calls) == 1


def test_scope_change_rejected_before_graph_apply(env):
    a = add(scope="eval:x")
    with db.session_scope() as session, pytest.raises(ValueError):
        runtime.apply(session, user_id=USER, scope="", facts=[a], report=report([a]), turn_key="x", generation=a.generation)


def test_disabled_recall_does_not_obtain_client(env, monkeypatch):
    monkeypatch.setattr(env.settings, "memory_graph_enabled", False)
    monkeypatch.setattr(runtime, "get_graph_client", lambda: (_ for _ in ()).throw(AssertionError("connected")))
    assert not runtime.recall(user_id=USER, scope="", query="甲")["enabled"]
