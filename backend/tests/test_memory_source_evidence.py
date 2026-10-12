import json
from datetime import timedelta
import pytest
from sqlalchemy import select
from tests.test_memory_search import search_env, _seed, U1
from app.memory import db, repo, episodes, search, evidence_retrieval as completion
from app.memory.tables import MemoryEpisodeRow


def seed_episode(env, *, scope="", turn="turn", content="Alex: I read The Silver Book yesterday."):
    mid = _seed(env, "Alex read a book")[0]
    now = db.utc_naive()
    with db.session_scope() as session:
        if scope:
            from app.memory.tables import MemoryItemRow
            session.get(MemoryItemRow, mid).scope = scope
            session.flush()
        repo.add_source(session, memory_id=mid, user_id=U1, scope=scope,
            conversation_id="chat", turn_id=turn, now=now)
        episodes.retain(session, user_id=U1, scope=scope, turn_id=turn, now=now,
            messages=[{"role":"user", "content":content, "message_id":"m1", "recorded_at":"2024-01-02"},
                      {"role":"tool", "content":"private SOP"}, {"role":"assistant", "content":"invented"}])
    return mid


@pytest.fixture
def sources(search_env, monkeypatch):
    monkeypatch.setattr(search_env.settings, "memory_source_store_enabled", True)
    monkeypatch.setattr(search_env.settings, "memory_source_search_enabled", True)
    monkeypatch.setattr(search_env.settings, "memory_evidence_rounds", 1)
    return search_env


def test_storage_excludes_tools_assistant_and_secrets(sources):
    seed_episode(sources)
    chunks, _ = episodes.corpus(U1, "")
    assert chunks and "Silver Book" in chunks[0]["text"]
    assert "private SOP" not in json.dumps(chunks) and "invented" not in json.dumps(chunks)
    with db.session_scope() as session:
        assert not episodes.retain(session, user_id=U1, scope="", turn_id="secret", now=db.utc_naive(),
            messages=[{"role":"user", "content":"api_key=secret-value"}])


def test_source_link_repair_requires_owned_current_backend_origin(sources):
    from app.memory.tables import MemoryItemRow
    ids=_seed(sources,"missing origin one","another turn","manually changed")
    with db.session_scope() as session:
        for mid,turn in zip(ids,["target","other","target"]):
            session.get(MemoryItemRow,mid).fact_context={"turn_id":turn}
        session.flush()
        repo.update_item(session,user_id=U1,memory_id=ids[2],new_text="withdrawn origin",actor="user",now=db.utc_naive())
        assert episodes.repair_source_links(session,user_id=U1,scope="",turn_id="target",
            now=db.utc_naive(),expected_generation=0)==1
        assert episodes.repair_source_links(session,user_id=U1,scope="",turn_id="target",
            now=db.utc_naive(),expected_generation=0)==0
        assert [x.turn_id for x in repo.list_sources(session,ids[0])]==["target"]
        assert not repo.list_sources(session,ids[1]) and not repo.list_sources(session,ids[2])
    with db.session_scope() as session:
        assert episodes.repair_source_links(session,user_id="other-user",scope="",turn_id="target",
            now=db.utc_naive(),expected_generation=0)==0


def test_scope_owner_expiry_and_generation_isolation(sources):
    seed_episode(sources, scope="eval:test")
    assert not episodes.corpus(U1, "")[0]
    assert not episodes.corpus("another-user", "eval:test")[0]
    with db.session_scope() as session:
        repo.bump_generation(session, U1, scope="eval:test", now=db.utc_naive())
    assert not episodes.corpus(U1, "eval:test")[0]
    from app.memory.errors import MemoryStaleGeneration
    with pytest.raises(MemoryStaleGeneration),db.session_scope() as session:
        episodes.retain(session,user_id=U1,scope="eval:test",turn_id="late",messages=[{"role":"user","content":"late source"}],
            now=db.utc_naive(),expected_generation=0)


def test_expired_episode_not_returned(sources):
    seed_episode(sources)
    with db.session_scope() as session:
        row = session.scalars(select(MemoryEpisodeRow)).one()
        row.expires_at = db.utc_naive() - timedelta(seconds=1)
    assert not episodes.corpus(U1, "")[0]


def test_manual_edit_revokes_raw_source_and_retry_cannot_restore(sources):
    mid=seed_episode(sources)
    with db.session_scope() as session:
        repo.update_item(session, user_id=U1, memory_id=mid, new_text="Alex did not read it",
                         actor="user", now=db.utc_naive())
        assert not episodes.retain(session, user_id=U1, scope="", turn_id="turn", now=db.utc_naive(),
            messages=[{"role":"user", "content":"original"}])
        assert session.scalars(select(MemoryEpisodeRow)).one().messages == []
    assert not episodes.corpus(U1, "")[0]


def test_retroactive_import_cannot_restore_never_retained_edited_source(sources,monkeypatch):
    monkeypatch.setattr(sources.settings,"memory_source_store_enabled",False)
    mid=seed_episode(sources)
    with db.session_scope() as session:
        repo.update_item(session,user_id=U1,memory_id=mid,new_text="corrected",actor="user",now=db.utc_naive())
    monkeypatch.setattr(sources.settings,"memory_source_store_enabled",True)
    with db.session_scope() as session:
        assert not episodes.retain(session,user_id=U1,scope="",turn_id="turn",now=db.utc_naive(),
            messages=[{"role":"user","content":"old source"}])


def test_delete_and_purge_remove_source_evidence(sources):
    mid=seed_episode(sources)
    with db.session_scope() as session:
        repo.delete_item(session, user_id=U1, memory_id=mid, actor="user", now=db.utc_naive())
    assert not episodes.corpus(U1, "")[0]
    with db.session_scope() as session:
        repo.purge_user(session, user_id=U1, scope="", now=db.utc_naive())
        assert not session.scalars(select(MemoryEpisodeRow)).all()


def test_raw_evidence_recovers_title_without_fact_write(sources):
    mid=seed_episode(sources)
    result=search.search(U1, "What book did Alex read?", capture_trace=True)
    assert "Silver Book" in search.format_evidence(result)
    assert result.counts["source_final"] == 1
    assert len(search.format_evidence(result)) <= sources.settings.memory_context_chars
    with db.session_scope() as session:
        assert repo.get_item(session,U1,mid).text == "Alex read a book"


def test_no_source_or_model_calls_when_disabled(search_env, monkeypatch):
    monkeypatch.setattr(episodes, "corpus", lambda *a: pytest.fail("unexpected source read"))
    search.search(U1, "question")


def test_planner_has_only_one_pass_and_two_anchored_queries(sources, monkeypatch):
    seed_episode(sources)
    monkeypatch.setattr(sources.settings, "memory_evidence_rounds", 99)
    monkeypatch.setattr(completion, "plan", lambda *a: completion.Plan(
        need_more=True, queries=["earlier book", "author"], missing=["title"]))
    calls=[]
    def once(*args, **kw):
        calls.append(args[1]); return search.SearchResult(query=args[1])
    result=completion.enrich(search.SearchResult(query="original question"), user_id=U1, scope="", once=once)
    assert len(calls)==2 and all("original question" in q for q in calls)
    assert result.counts["completion_rounds"]==3


def test_completion_failure_preserves_first_pass(sources, monkeypatch):
    monkeypatch.setattr(episodes, "corpus", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    result=search.search(U1, "book")
    assert any("来源补检索失败" in n for n in result.degraded)


def test_source_revalidation_drops_concurrent_revocation(sources, monkeypatch):
    seed_episode(sources)
    monkeypatch.setattr(episodes, "validate", lambda *a: [])
    result=search.search(U1,"book")
    assert "Silver Book" not in search.format_evidence(result)
    assert result.counts["source_final"] == 0


def test_supplemental_facts_share_budget_even_without_raw_snippets(sources,monkeypatch):
    monkeypatch.setattr(sources.settings,"memory_evidence_rounds",2)
    monkeypatch.setattr(sources.settings,"memory_context_chars",300)
    ids=_seed(sources,"alpha "*18,"beta "*18,"gamma "*18)
    snapshots={}
    with db.session_scope() as session:
        snapshots={x.id:x for x in repo.list_by_ids(session,ids)}
    rows=[(mid,1.,1.,1,None,"rerank") for mid in ids]
    hits,_=search._finalize(rows,limit=3,budget=1000,snapshots=snapshots)
    monkeypatch.setattr(completion,"plan",lambda *a:completion.Plan(need_more=True,queries=["more"],missing=["other facts"]))
    result=completion.enrich(search.SearchResult(query="original",hits=hits[:1]),user_id=U1,scope="",
        once=lambda *a,**kw:search.SearchResult(query="more",hits=hits[1:]))
    assert len(search.format_evidence(result))<=300
    assert result.trace["completion"]["fact_budget_dropped"]


def test_source_recall_handles_inflections_without_using_speaker_as_main_topic(sources):
    corpus=[{"id":"a","text":"Alex visited a museum","speaker":"Alex"},
            {"id":"b","text":"Alex Alex Alex discussed work","speaker":"Alex"}]
    assert completion.candidates("What museums did Alex visit?",corpus)[0]["id"]=="a"
