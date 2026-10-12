from dataclasses import replace
from datetime import datetime
import pytest
from tests.test_memory_search import search_env,_seed,U1
from tests.test_memory_source_evidence import sources,seed_episode
from app.memory import search,db,repo,episodes,rerank
from app.memory import evidence_selection as selection,evidence_retrieval as adaptive


def hit(mid,value,context=None):
    return search.Evidence(memory_id=mid,text=value,score=1,origin='rerank',rrf=1,vector_rank=1,
        bm25_rank=1,updated_at=datetime(2024,1,1),thread_id='t',revision=1,fact_context=context or {})


def test_compact_selection_budgets_labels_and_keeps_whole_records(search_env,monkeypatch):
    monkeypatch.setattr(search_env.settings,'memory_context_chars',90)
    r=search.SearchResult(query='list facts',candidates=[hit('a','x'*60),hit('b','y'*60)])
    selection.select_facts(r)
    assert len(search.format_evidence(r))<=90
    assert r.hits[0].text=='x'*60 and not r.hits[0].trimmed
    assert r.trace['selection']['dropped_for_budget']==['b']


def test_preserves_missing_precision_and_relative_time(search_env):
    h=hit('a','Alex reviewed a report',{'time':{'raw':'last week','anchor':'2024-06-04','precision':'week'},
        'structured_graph':{'graph':{'entities':[{'ref':'a','name':'Alex'},{'ref':'b','name':'Blair'}],
            'relations':[{'subject':'a','relation':'reviewed with','object_entity':'b'}]}}})
    result=selection.render(h,1)
    assert 'Blair' in result and 'reviewed with' in result and 'last week' in result and '2024-06-04' in result


def test_source_merge_does_not_truncate_new_query_candidates_before_ranking():
    a=[{'id':'a'},{'id':'b'}];b=[{'id':'new'},{'id':'a'}]
    assert [x['id'] for x in adaptive.merge_pools([a,b],2)]==['a','new']


def test_source_view_keeps_centre_and_removes_duplicate_speaker_prefix():
    messages=[{'message_id':'m1','speaker':'Alex','recorded_at':'date','content':'Alex: unrelated'},
        {'message_id':'m2','speaker':'Blair','recorded_at':'date','content':'Blair: I travelled to North.'}]
    x=adaptive.source_view({'center_message_id':'m2','messages':messages})
    assert x['message_ids']==['m2'] and 'Blair: Blair:' not in x['text'] and 'unrelated' not in x['text']


def test_chinese_referring_start_keeps_previous_utterance_without_ascii_word_boundary():
    messages=[{'message_id':'m1','speaker':'小安','recorded_at':'date','content':'昨天参加了活动。'},
        {'message_id':'m2','speaker':'小安','recorded_at':'date','content':'这让我很开心。'}]
    x=adaptive.source_view({'center_message_id':'m2','messages':messages})
    assert x['message_ids']==['m1','m2'] and '活动' in x['text']


def test_direct_temporal_evidence_skips_source_and_planner(sources,monkeypatch):

    r=search.SearchResult(query='When did Alex travel?',hits=[hit('a','Alex travelled',{'time':{'raw':'yesterday'}})])
    monkeypatch.setattr(episodes,'corpus',lambda *a:pytest.fail('unexpected corpus read'))
    out=adaptive.enrich(r,user_id=U1,scope='',once=lambda *a,**k:pytest.fail('unexpected query'))
    assert out is r and out.counts['completion_rounds']==0


def test_sources_never_displace_already_selected_facts(sources,monkeypatch):

    ids=_seed(sources,'Alex travelled North','Alex travelled West','Alex travelled South')
    with db.session_scope() as s:items=repo.list_by_ids(s,ids)
    hits=[replace(hit(x.id,x.text),generation=x.generation,content_hash=x.content_hash,meta_version=x.meta_version) for x in items]
    r=search.SearchResult(query='Where has Alex travelled?',hits=hits,candidates=hits,selection_limit=3)
    selection.select_facts(r)
    monkeypatch.setattr(sources.settings,'memory_context_chars',len(r.source_context)+10)
    raw={'id':'raw','episode_id':'episode','turn_id':'turn','message_ids':['m1'],'text':'Alex travelled somewhere else with many details '*20}
    monkeypatch.setattr(episodes,'corpus',lambda *a:([raw],False))
    monkeypatch.setattr(episodes,'validate',lambda *a:a[-1])
    monkeypatch.setattr(rerank,'configured',lambda:False)
    out=adaptive.enrich(r,user_id=U1,scope='',once=lambda *a,**k:None)
    assert {h.memory_id for h in out.hits}==set(ids)
    assert not out.trace['completion']['selected']
    assert out.trace['completion']['source_rejections'][0]['reason']=='remaining_budget'
    assert len(out.source_context)<=sources.settings.memory_context_chars


def test_opt_in_search_exposes_more_candidates_than_final_limit(search_env,monkeypatch):

    ids=_seed(search_env,'first topic','second topic','third topic')
    search_env.dense.ids=ids
    result=search.search(U1,'topic',top_k=1,capture_trace=True)
    assert len(result.hits)==1 and len(result.candidates)==3 and result.selection_limit==1
    assert result.trace['selection']['body_chars']==len(search.format_evidence(result))


def test_diagnostic_marks_incomplete_path_without_presenting_it_as_complete(search_env):
    r=search.SearchResult(query='facts',candidates=[hit('a','one fact')],
        trace={'graph':{'paths':[{'memory_ids':['a','missing']}]}})
    selection.select_facts(r)
    assert not r.trace['selection']['complete_paths']
    assert not r.hits
    assert r.trace['selection']['rejected_paths'][0]['reason']=='missing_validated_member'


def test_supplemented_pool_revalidates_deletion_before_final_selection(sources,monkeypatch):

    mid=seed_episode(sources)
    with db.session_scope() as session:item=repo.get_item(session,U1,mid)
    h=replace(hit(mid,item.text),generation=item.generation,content_hash=item.content_hash,meta_version=item.meta_version)
    monkeypatch.setattr(episodes,'corpus',lambda *a:([],False))
    with db.session_scope() as session:repo.delete_item(session,user_id=U1,memory_id=mid,actor='user',now=db.utc_naive())
    out=adaptive.enrich(search.SearchResult(query='list all remembered items',hits=[h],candidates=[h]),
        user_id=U1,scope='',once=lambda *a,**k:None)
    assert not out.hits and not search.format_evidence(out)
