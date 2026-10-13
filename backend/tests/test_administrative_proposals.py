"""Chat draft/confirmation boundaries and transactional behavior, no paid services."""
from datetime import timedelta
import pytest
from sqlalchemy import select
from app.matters import proposals, service, schemas
from app.matters.tables import Matter, Event, Proposal
from tests.test_administrative_matters import storage

def proposal(**kwargs):
    return proposals.propose('owner', 'thread', 'create',
        {'title':'差旅报销', 'details':{'amount':'320.10','purpose':'参会', **kwargs}})

def test_confirmation_is_required_and_idempotent(storage):
    p=proposal()
    assert service.list_matters('owner')==[]
    assert proposal()['id']==p['id']
    with pytest.raises(service.NotFound):proposals.decide('other',p['id'],True)
    result=proposals.decide('owner',p['id'],True)
    assert result['status']=='confirmed'
    assert proposals.decide('owner',p['id'],True)==result
    assert len(service.list_matters('owner'))==1
    with storage() as s:assert len(s.scalars(select(Event)).all())==1

def test_missing_core_fields_and_invalid_money_do_not_create(storage):
    for details in [{}, {'amount':'-1','purpose':'用品'}, {'amount':'3','purpose':''}]:
        with pytest.raises(service.Invalid):proposals.propose('owner','thread','create',{'title':'报销','details':details})
    with storage() as s:assert s.scalars(select(Proposal)).all()==[]

def test_reject_expiry_and_supersession(storage):
    p=proposal();new=proposal(amount='400')
    with pytest.raises(service.Conflict):proposals.decide('owner',p['id'],True)
    assert proposals.decide('owner',new['id'],False)['status']=='rejected'
    with pytest.raises(service.Conflict):proposals.decide('owner',new['id'],True)
    p=proposal(amount='401')
    with storage() as s:s.get(Proposal,p['id']).expires_at=service.db.utc_naive()-timedelta(seconds=1)
    with pytest.raises(service.Conflict):proposals.decide('owner',p['id'],True)
    assert service.list_matters('owner')==[]

def test_edit_merges_fields_and_stale_confirmation_cannot_overwrite(storage):
    item=proposals.decide('owner',proposal()['id'],True)['result']
    p=proposals.propose('owner','thread','edit',{'details':{'amount':'400'}},item['id'])
    assert p['payload']['request']['details']['purpose']=='参会'
    service.mutate('owner',item['id'],schemas.Edit(title='新版',details=item['details'],request_id='edit',revision=1),'edit')
    with pytest.raises(service.Conflict):proposals.decide('owner',p['id'],True)
    assert service.get('owner',item['id'])['title']=='新版'
    assert proposals.listing('owner','thread')[0]['status']=='pending'
    assert proposals.listing('other','thread')==[]

def test_invalid_transition_rolls_back_confirmation(storage):
    item=proposals.decide('owner',proposal()['id'],True)['result']
    p=proposals.propose('owner','thread','event',{'kind':'pay','description':'到账','source_ref':'通知'},item['id'])
    with pytest.raises(service.Invalid):proposals.decide('owner',p['id'],True)
    assert service.get('owner',item['id'])['status']=='preparing'
    assert proposals.listing('owner','thread')[0]['status']=='pending'

def test_material_event_and_delete_chain(storage):
    item=proposals.decide('owner',proposal(policy_ref='sop:travel',required_materials=['发票'])['id'],True)['result']
    p=proposals.propose('owner','thread','link',{'kind':'material','title':'发票','state':'provided','source_ref':'用户提供：文件1'},item['id'])
    proposals.decide('owner',p['id'],True)
    assert not service.get('owner',item['id'])['missing_materials']
    p=proposals.propose('owner','thread','event',{'kind':'submit','description':'用户确认已提交'},item['id'])
    assert proposals.decide('owner',p['id'],True)['result']['status']=='submitted'
    p=proposals.propose('owner','thread','delete',{},item['id'])
    proposals.decide('owner',p['id'],True)
    with pytest.raises(service.NotFound):service.get('owner',item['id'])

def test_model_has_no_confirmation_tool_and_hidden_identity(storage):
    from app.graph.tools import TOOLS, propose_administrative_change
    assert not any('confirm' in t.name for t in TOOLS)
    assert 'config' not in propose_administrative_change.tool_call_schema.model_fields
    assert 'user_id' not in propose_administrative_change.tool_call_schema.model_fields
    out=propose_administrative_change.invoke({'operation':'create','payload':{'title':'报销','details':{'amount':'30','purpose':'用品'}}},
        config={'configurable':{'authenticated_user_id':'owner','thread_id':'thread'}})
    import json
    assert json.loads(out)['status']=='pending'
    assert not service.list_matters('owner')

def test_different_matters_do_not_replace_each_other(storage):
    first=proposal()
    second=proposals.propose('owner','thread','create',{'title':'办公用品报销','details':{'amount':'40','purpose':'耗材'}})
    assert proposals.decide('owner',first['id'],True)['status']=='confirmed'
    assert proposals.decide('owner',second['id'],True)['status']=='confirmed'
    assert len(service.list_matters('owner'))==2

def test_rejected_proposal_can_be_requested_again(storage):
    p=proposal();proposals.decide('owner',p['id'],False)
    new=proposal()
    assert new['id']!=p['id'] and new['status']=='pending'

@pytest.mark.asyncio
async def test_react_sop_to_proposal_then_user_confirmation(monkeypatch):
    """Run the actual graph and ToolNode, scripted LLM, isolated SQLite storage."""
    from contextlib import contextmanager
    from types import SimpleNamespace
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from langchain_core.messages import AIMessage, HumanMessage
    from app.matters.tables import Base
    from app.graph import nodes, tools
    from app.graph.builder import build_graph
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions=sessionmaker(engine,expire_on_commit=False)
    @contextmanager
    def scope():
        with sessions() as s:
            with s.begin():yield s
    monkeypatch.setattr(service.db,'session_scope',scope)
    meta=SimpleNamespace(id='office',name='办公用品报销',description='办公用品',triggers=['办公用品'])
    monkeypatch.setattr(tools.loader,'get_catalog',lambda:[meta])
    monkeypatch.setattr(tools.loader,'get_skill',lambda _: (meta,'# 办公用品报销\n准备发票和付款凭证。'))
    steps=[('list_sops',{}),('get_sop',{'skill_id':'office'}),
        ('propose_administrative_change',{'operation':'create','payload':{'title':'办公用品报销',
         'details':{'amount':'320.10','purpose':'办公用品','policy_ref':'sop:office','required_materials':['发票','付款凭证']}}})]
    class Model:
        def bind_tools(self,bound):
            assert 'propose_administrative_change' in [t.name for t in bound]
            return self
        async def ainvoke(self,messages):
            if steps:
                name,args=steps.pop(0)
                return AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':name}])
            return AIMessage(content='已整理，请在卡片中核对并确认。')
    monkeypatch.setattr(nodes,'get_llm',lambda:Model())
    try:
        result=await build_graph().ainvoke({'messages':[HumanMessage(content='办公用品报销320.10元')]},
             config={'configurable':{'authenticated_user_id':'owner','thread_id':'chat'}})
        assert '确认' in result['messages'][-1].content
        assert not service.list_matters('owner')
        pending=proposals.listing('owner','chat')
        assert len(pending)==1
        confirmed=proposals.decide('owner',pending[0]['id'],True)
        item=service.get('owner',confirmed['result']['id'])
        assert item['missing_materials']==['发票','付款凭证']
        assert len(item['events'])==1
    finally:
        engine.dispose()
