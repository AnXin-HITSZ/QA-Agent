"""Opt-in actual MySQL/Neo4j checks; random owner, deterministic cleanup, no LLM."""
import os,uuid
import pytest
from sqlalchemy import delete
from app.matters import schemas,service,projection
from app.matters.tables import Matter,Link,Event,Projection
from app.memory import db
from app.config import get_settings

@pytest.mark.skipif(not os.getenv('ADMIN_TEST_REAL_SERVICES'),reason='isolated real services not opted in')
def test_real_mysql_graph_publish_edit_delete_and_fencing():
    from sqlalchemy import text
    with db.session_scope() as session:
        assert get_settings().app_env=='dev' and session.scalar(text('SELECT DATABASE()'))=='qa_agent_dev'
    uid=str(uuid.uuid4());mid=None
    try:
        request=schemas.Create(title='integration fixture',request_id='create',details={'amount':'12.30','purpose':'test','project':'test project'})
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            items=list(pool.map(lambda _:service.create(uid,request),range(2)))
        assert items[0]['id']==items[1]['id']
        item=items[0];mid=item['id']
        assert projection.sync_one(uid,mid)
        assert service.graph(uid,mid)['source']=='neo4j_validated'
        assert projection.audit(projection.snapshot(uid,mid))
        client=projection.driver()
        with client.session(database=get_settings().neo4j_database) as session:
            session.run('MATCH (n:QAAdministrativeObject {user_id:$uid}) SET n.title=$title',uid=uid,title='drift').consume()
        assert not projection.audit(projection.snapshot(uid,mid))
        projection._audit_at=0;projection._audit_after=''
        projection.reconcile()
        with db.session_scope() as session:assert session.get(Projection,mid).published_revision==0
        assert projection.sync_one(uid,mid)
        assert projection.audit(projection.snapshot(uid,mid))
        old=projection.snapshot(uid,mid)
        item=service.mutate(uid,mid,schemas.Action(revision=1,request_id='note',kind='note',description='test event'),'event')
        assert projection.sync_one(uid,mid)
        projection.publish(old)
        assert projection.read(uid,mid,2) is not None
        service.mutate(uid,mid,schemas.Delete(revision=2,request_id='delete'),'delete')
        assert projection.sync_one(uid,mid)
        projection.publish(old)
        assert projection.read(uid,mid,1) is None
        client=projection.driver()
        with client.session(database=get_settings().neo4j_database) as session:
            assert session.run('MATCH (n:QAAdministrativeObject {user_id:$uid}) RETURN count(n) AS n',uid=uid).single()['n']==0
    finally:
        client=projection.driver()
        if client:
            with client.session(database=get_settings().neo4j_database) as session:
                session.run('MATCH (n {user_id:$uid}) WHERE n:QAAdministrativeMatter OR n:QAAdministrativeObject DETACH DELETE n',uid=uid).consume()
                assert session.run('MATCH (n {user_id:$uid}) WHERE n:QAAdministrativeMatter OR n:QAAdministrativeObject RETURN count(n) AS n',uid=uid).single()['n']==0
        with db.session_scope() as session:
            for table in (Event,Link,Projection,Matter):session.execute(delete(table).where(table.user_id==uid))
        projection.stop()
