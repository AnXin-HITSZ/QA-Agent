"""A durable, revision-fenced administrative graph projection, not chat recall."""
import threading
from sqlalchemy import select
from app.config import get_settings
from app.memory import db
from app.matters.tables import Matter, Link, Event, Projection

_stop=threading.Event()
_thread=None
_driver=None
_lock=threading.Lock()
_audit_after=''
_audit_at=0.0

def driver():
    global _driver
    s=get_settings()
    if not s.memory_graph_enabled:return None
    with _lock:
        if _driver is None:
            from neo4j import GraphDatabase
            _driver=GraphDatabase.driver(s.neo4j_uri,auth=(s.neo4j_username,s.neo4j_password),
                connection_timeout=s.memory_graph_connect_timeout_seconds,
                connection_acquisition_timeout=s.memory_graph_connect_timeout_seconds,
                max_transaction_retry_time=s.memory_graph_query_timeout_seconds,
                max_connection_pool_size=s.memory_graph_max_pool_size)
        return _driver

def snapshot(uid,mid):
    with db.session_scope() as session:
        row=session.scalar(select(Matter).where(Matter.id==mid,Matter.user_id==uid))
        if not row:return None
        nodes=[];edges=[]
        if not row.deleted:
            for link in session.scalars(select(Link).where(Link.user_id==uid,Link.matter_id==mid,Link.deleted==0)):
                nodes.append({'id':link.id,'kind':link.kind,'title':link.title})
                edges.append({'id':link.id,'source':mid,'target':link.id,'kind':link.kind})
                if link.kind=='task' and link.details.get('origin_event'):
                    edges.append({'id':link.id+':origin','source':link.id,'target':link.details['origin_event'],'kind':'origin_event'})
            for event in session.scalars(select(Event).where(Event.user_id==uid,Event.matter_id==mid)):
                nodes.append({'id':event.id,'kind':'event','title':event.description})
                edges.append({'id':event.id,'source':event.id,'target':mid,'kind':'event'})
            for key,kind in (('project','project'),('handler','person')):
                if row.details.get(key):
                    identifier=mid+':'+key
                    nodes.append({'id':identifier,'kind':kind,'title':row.details[key]})
                    edges.append({'id':identifier,'source':mid,'target':identifier,'kind':kind})
        return {'user_id':uid,'id':mid,'title':row.title if not row.deleted else '', 'status':row.status,
                'deleted':bool(row.deleted),'revision':row.revision,'nodes':nodes,'edges':edges}

def initialize():
    client=driver()
    if client is None:raise RuntimeError('事务图未启用')
    from neo4j import Query
    with client.session(database=get_settings().neo4j_database) as session:
        for statement in (
            'CREATE CONSTRAINT qa_administrative_matter_identity IF NOT EXISTS FOR (m:QAAdministrativeMatter) REQUIRE (m.user_id,m.id) IS UNIQUE',
            'CREATE CONSTRAINT qa_administrative_object_identity IF NOT EXISTS FOR (n:QAAdministrativeObject) REQUIRE (n.user_id,n.id) IS UNIQUE',
            'CREATE INDEX qa_administrative_objects_owner IF NOT EXISTS FOR (n:QAAdministrativeObject) ON (n.user_id,n.matter_id)',
        ):session.run(Query(statement,timeout=get_settings().memory_graph_query_timeout_seconds)).consume()

def publish(data):
    client=driver()
    if not client:return False
    from neo4j import unit_of_work
    @unit_of_work(timeout=get_settings().memory_graph_query_timeout_seconds)
    def write(tx):
        # SET acquires a write lock BEFORE checking revision, serializing publishers.
        row=tx.run('MERGE (m:QAAdministrativeMatter {id:$id,user_id:$uid}) '
            'ON CREATE SET m.revision=0 SET m.lock_marker=coalesce(m.lock_marker,0)+1 '
            'RETURN m.revision AS revision',id=data['id'],uid=data['user_id']).single()
        if row['revision']>data['revision']:return
        params={'id':data['id'],'uid':data['user_id']}
        tx.run('MATCH (n:QAAdministrativeObject {matter_id:$id,user_id:$uid}) DETACH DELETE n',**params).consume()
        tx.run('MATCH (m:QAAdministrativeMatter {id:$id,user_id:$uid}) '
            'SET m.revision=$revision,m.deleted=$deleted,m.title=$title,m.status=$status',
            **params,revision=data['revision'],deleted=data['deleted'],title=data['title'],status=data['status']).consume()
        if data['deleted']:return  # Keep a non-sensitive tombstone to reject delayed resurrection.
        tx.run('UNWIND $nodes AS node CREATE (n:QAAdministrativeObject) '
            'SET n.id=node.id,n.kind=node.kind,n.title=node.title,n.matter_id=$id,n.user_id=$uid',**params,nodes=data['nodes']).consume()
        tx.run('UNWIND $edges AS edge MATCH (a {id:edge.source,user_id:$uid}), (b {id:edge.target,user_id:$uid}) '
            'WHERE (a:QAAdministrativeMatter OR a:QAAdministrativeObject) '
            'AND (b:QAAdministrativeMatter OR b:QAAdministrativeObject) '
            'CREATE (a)-[r:QA_ADMIN_LINK]->(b) SET r.kind=edge.kind,r.matter_id=$id,r.user_id=$uid',**params,edges=data['edges']).consume()
    with client.session(database=get_settings().neo4j_database) as session:
        session.execute_write(write)
    return True

def sync_one(uid,mid):
    data=snapshot(uid,mid)
    if data is None:return False
    try:
        if not publish(data):return False
        with db.session_scope() as session:
            op=session.get(Projection,mid)
            if op and op.user_id==uid and op.revision==data['revision']:
                op.published_revision=data['revision'];op.last_error='';op.attempts=0
        return True
    except Exception as exc:
        with db.session_scope() as session:
            op=session.get(Projection,mid)
            if op and op.user_id==uid and op.revision==data['revision']:
                op.last_error=type(exc).__name__;op.attempts+=1;op.updated_at=db.utc_naive()
        return False

def read(uid,mid,revision):
    """Read an exact business revision; the caller validates returned objects."""
    client=driver()
    if client is None:return None
    from neo4j import unit_of_work
    @unit_of_work(timeout=get_settings().memory_graph_query_timeout_seconds)
    def fetch(tx):
        root=tx.run('MATCH (m:QAAdministrativeMatter {id:$id,user_id:$uid}) RETURN m.revision AS revision,m.deleted AS deleted',id=mid,uid=uid).single()
        if not root or root['deleted'] or root['revision']!=revision:return None
        nodes=[dict(x) for x in tx.run('MATCH (n:QAAdministrativeObject {matter_id:$id,user_id:$uid}) RETURN n.id AS id,n.kind AS kind,n.title AS title',id=mid,uid=uid)]
        edges=[dict(x) for x in tx.run('MATCH (a)-[r:QA_ADMIN_LINK {matter_id:$id,user_id:$uid}]->(b) RETURN a.id AS source,b.id AS target,r.kind AS kind',id=mid,uid=uid)]
        return {'nodes':nodes,'edges':edges}
    with client.session(database=get_settings().neo4j_database) as session:return session.execute_read(fetch)

def audit(data):
    """Compare object values and relationships, not merely totals."""
    client=driver()
    if client is None:raise RuntimeError('事务图未启用')
    from neo4j import unit_of_work
    @unit_of_work(timeout=get_settings().memory_graph_query_timeout_seconds)
    def fetch(tx):
        uid,mid=data['user_id'],data['id']
        root=tx.run('MATCH (m:QAAdministrativeMatter {id:$id,user_id:$uid}) RETURN m.revision AS revision,m.deleted AS deleted,m.title AS title,m.status AS status',id=mid,uid=uid).single()
        nodes=[dict(x) for x in tx.run('MATCH (n:QAAdministrativeObject {matter_id:$id,user_id:$uid}) RETURN n.id AS id,n.kind AS kind,n.title AS title',id=mid,uid=uid)]
        edges=[dict(x) for x in tx.run('MATCH (a)-[r:QA_ADMIN_LINK {matter_id:$id,user_id:$uid}]->(b) RETURN a.id AS source,b.id AS target,r.kind AS kind',id=mid,uid=uid)]
        import json
        same=lambda a,b:sorted(json.dumps(x,sort_keys=True) for x in a)==sorted(json.dumps(x,sort_keys=True) for x in b)
        return bool(root and all(root[key]==data[key] for key in ('revision','deleted','title','status')) and
                    same(nodes,data['nodes']) and same(edges,[{k:v for k,v in e.items() if k!='id'} for e in data['edges']]))
    with client.session(database=get_settings().neo4j_database) as session:return session.execute_read(fetch)

def reconcile():
    """Bounded cursor-based repair of current-version drift, no embedding calls."""
    global _audit_after,_audit_at
    import time
    if time.monotonic()<_audit_at:return
    _audit_at=time.monotonic()+300
    with db.session_scope() as session:
        rows=[(r.user_id,r.id,r.revision) for r in session.scalars(select(Projection).where(
            Projection.id>_audit_after,Projection.revision==Projection.published_revision)
            .order_by(Projection.id).limit(min(20,max(1,get_settings().memory_graph_sync_batch))))]
    if not rows:_audit_after='';return
    for uid,mid,revision in rows:
        if _stop.is_set():break
        data=snapshot(uid,mid)
        if data and not audit(data):
            with db.session_scope() as session:
                op=session.get(Projection,mid)
                if op and op.user_id==uid and op.revision==revision:
                    op.published_revision=0;op.last_error='projection_drift';op.updated_at=db.utc_naive()
        _audit_after=mid

def tick():
    if not get_settings().memory_graph_enabled or not db.configured():return
    with db.session_scope() as session:
        rows=[(x.user_id,x.id) for x in session.scalars(select(Projection).where(Projection.revision!=Projection.published_revision)
            .order_by(Projection.updated_at,Projection.id).limit(max(1,get_settings().memory_graph_sync_batch)))]
    for uid,mid in rows:
        if _stop.is_set():break
        sync_one(uid,mid)
    reconcile()

def _loop():
    import logging
    while not _stop.wait(30):
        try:tick()
        except Exception as exc:logging.getLogger(__name__).warning('事务图同步暂不可用(%s)',type(exc).__name__)

def start():
    global _thread
    if not get_settings().memory_graph_enabled:return
    if _thread and _thread.is_alive():return
    _stop.clear();_thread=threading.Thread(target=_loop,name='administrative-graph',daemon=True);_thread.start()

def stop():
    global _driver
    _stop.set()
    if _thread:
        _thread.join(timeout=30)
        if _thread.is_alive():return  # Do not close a driver still in use by the worker.
    with _lock:
        if _driver:_driver.close();_driver=None
