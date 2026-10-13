"""Meaningful business/ownership/version regressions, no external services."""
from contextlib import contextmanager
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.matters import service, schemas, projection
from app.matters.tables import Base, Event, Matter, Projection

@pytest.fixture
def storage(monkeypatch):
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    sessions=sessionmaker(engine,expire_on_commit=False)
    @contextmanager
    def scope():
        with sessions() as session:
            with session.begin():yield session
    monkeypatch.setattr(service.db,'session_scope',scope)
    return scope

def create(request='create',**details):
    return service.create('owner',schemas.Create(title='办公用品报销',request_id=request,details=details))

def test_precise_amount_ownership_and_replay(storage):
    item=create(amount='320.10')
    assert item['details']['amount']=='320.10'
    assert create(amount='320.10')['id']==item['id']
    with pytest.raises(service.NotFound):service.get('other',item['id'])
    with pytest.raises(service.Conflict):create(amount='999.10')

def test_requirements_have_policy_source(storage):
    with pytest.raises(service.Invalid):create(required_materials=['发票'])
    item=create(amount='320',purpose='办公用品',policy_ref='sop:office@1',required_materials=['发票','付款凭证'])
    details=service.get('owner',item['id'])
    assert details['missing_materials']==['发票','付款凭证']
    assert len([x for x in details['links'] if x['kind']=='task'])==2

def test_reported_material_not_provided_and_no_duplicate_tasks(storage):
    item=create(amount='320',purpose='办公用品',policy_ref='sop:office@1',required_materials=['发票'])
    item=service.mutate('owner',item['id'],schemas.LinkInput(revision=item['revision'],request_id='link1',kind='material',title='发票',state='reported'),'link')
    assert service.get('owner',item['id'])['missing_materials']==['发票']
    with pytest.raises(service.Invalid):
        service.mutate('owner',item['id'],schemas.Action(revision=item['revision'],request_id='submit1',kind='submit',description='已提交'),'event')
    item=service.mutate('owner',item['id'],schemas.LinkInput(revision=item['revision'],request_id='link2',kind='material',title='发票',state='provided',source_ref='fixture:invoice'),'link')
    data=service.get('owner',item['id'])
    assert data['missing_materials']==[]
    tasks=[x for x in data['links'] if x['kind']=='task']
    assert len(tasks)==1 and tasks[0]['details']['state']=='done'

def test_state_transitions_audit_and_conflict(storage):
    item=create(amount='320',purpose='办公用品')
    with pytest.raises(service.Invalid):
        service.mutate('owner',item['id'],schemas.Action(revision=1,request_id='pay',kind='pay',description='已支付',source_ref='receipt:1'),'event')
    item=service.mutate('owner',item['id'],schemas.Action(revision=1,request_id='submit',kind='submit',description='用户确认提交'),'event')
    assert item['status']=='submitted'
    with pytest.raises(service.Conflict):
        service.mutate('owner',item['id'],schemas.Action(revision=1,request_id='stale',kind='note',description='旧版本'),'event')
    with pytest.raises(service.Invalid):
        service.mutate('owner',item['id'],schemas.Action(revision=2,request_id='approve',kind='approve',description='已批准'),'event')
    item=service.mutate('owner',item['id'],schemas.Action(revision=2,request_id='return',kind='return',description='需要补充证明',source_ref='notice:1'),'event')
    old=service.get('owner',item['id'])['events']
    service.mutate('owner',item['id'],schemas.Action(revision=3,request_id='correction',kind='correct',description='更正说明'),'event')
    assert service.get('owner',item['id'])['events'][:len(old)]==old

def test_delete_persists_projection_intent_and_no_graph_leak(storage):
    item=create()
    with pytest.raises(service.NotFound):service.graph('other',item['id'])
    service.mutate('owner',item['id'],schemas.Delete(revision=1,request_id='delete'),'delete')
    replay=service.mutate('owner',item['id'],schemas.Delete(revision=1,request_id='delete'),'delete')
    assert replay['revision']==2
    with pytest.raises(service.NotFound):service.get('owner',item['id'])
    snapshot=projection.snapshot('owner',item['id'])
    assert snapshot['deleted'] and not snapshot['nodes'] and snapshot['title']==''
    with storage() as session:
        op=session.get(Projection,item['id'])
        assert op.revision==2 and op.published_revision==0
        assert len(session.scalars(select(Event)).all())==2

def test_projection_failure_does_not_rollback_fact(storage,monkeypatch):
    item=create()
    def failed(_):raise RuntimeError('offline')
    monkeypatch.setattr(projection,'publish',failed)
    assert not projection.sync_one('owner',item['id'])
    assert service.get('owner',item['id'])['projection']['error']=='RuntimeError'

def test_delayed_publish_cannot_ack_new_revision(storage,monkeypatch):
    item=create()
    def concurrent(_):
        service.mutate('owner',item['id'],schemas.Action(revision=1,request_id='note',kind='note',description='补充'),'event')
        return True
    monkeypatch.setattr(projection,'publish',concurrent)
    assert projection.sync_one('owner',item['id'])
    with storage() as session:
        op=session.get(Projection,item['id'])
        assert op.revision==2 and op.published_revision==0

def test_disabled_graph_never_loads_driver(monkeypatch):
    from app.config import Settings
    monkeypatch.setattr(projection,'get_settings',lambda:Settings(_env_file=None,memory_graph_enabled=False))
    assert projection.driver() is None
