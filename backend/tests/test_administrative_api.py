from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.routes import matters
from app.auth.deps import auth_ready, require_user
from types import SimpleNamespace
import pytest

@pytest.fixture
def client(monkeypatch):
    app=FastAPI();app.include_router(matters.router)
    app.dependency_overrides[auth_ready]=lambda:None
    app.dependency_overrides[require_user]=lambda:SimpleNamespace(user_id='trusted-owner')
    with TestClient(app) as client:yield client

def test_caller_cannot_supply_another_owner(client,monkeypatch):
    called=[]
    monkeypatch.setattr(matters.service,'create',lambda uid,req:called.append(uid) or {'id':'new'})
    assert client.post('/api/v1/matters',json={'title':'报销','request_id':'one','user_id':'other'}).status_code==422
    assert not called
    assert client.post('/api/v1/matters',json={'title':'报销','request_id':'one'}).status_code==200
    assert called==['trusted-owner']

def test_not_found_and_conflict_are_distinct(client,monkeypatch):
    def missing(*a):raise matters.service.NotFound()
    monkeypatch.setattr(matters.service,'get',missing)
    assert client.get('/api/v1/matters/unknown').status_code==404
    def conflict(*a):raise matters.service.Conflict('refresh')
    monkeypatch.setattr(matters.service,'mutate',conflict)
    assert client.patch('/api/v1/matters/id',json={'revision':1,'request_id':'one','title':'报销','details':{}}).status_code==409

def test_tools_hide_identity_from_model_and_require_trusted_config(monkeypatch):
    from app.graph.tools import list_administrative_matters, get_administrative_matter
    assert 'config' not in list_administrative_matters.tool_call_schema.model_fields
    assert 'user_id' not in get_administrative_matter.tool_call_schema.model_fields
    seen=[]
    monkeypatch.setattr(matters.service,'list_matters',lambda uid,**kwargs:seen.append(uid) or [])
    result=list_administrative_matters.invoke({},config={'configurable':{'authenticated_user_id':'trusted'}})
    import json
    assert json.loads(result)['items']==[] and seen==['trusted']

def test_administrative_schema_matches_numbered_migration():
    from tests import migrationkit as mig
    model=mig.model_tables('app.matters.tables');sql=mig.migration_tables()
    for name,table in model.items():mig.compare_tables(sql[name],table,label=name)

def test_proposal_endpoints_are_authenticated_and_use_server_owner(client,monkeypatch):
    seen=[]
    monkeypatch.setattr(matters.proposals,'listing',lambda uid,tid:seen.append((uid,tid)) or [])
    assert client.get('/api/v1/matters/proposals?thread_id=chat').json()=={'items':[]}
    assert seen==[('trusted-owner','chat')]
    monkeypatch.setattr(matters.proposals,'decide',lambda uid,pid,confirm:seen.append((uid,pid,confirm)) or {'status':'confirmed'})
    assert client.post('/api/v1/matters/proposals/id/confirm').status_code==200
    assert seen[-1]==('trusted-owner','id',True)
