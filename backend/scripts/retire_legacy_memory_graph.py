"""Explicit owner-scoped withdrawal of old generic graph data, no model calls."""
import argparse,sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.run_memory_eval import settings_and_environment
from app.memory import db,repo
from app.memory.graph import runtime
from app.memory.tables import MemoryJobRow
from sqlalchemy import select

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--user-id',required=True);p.add_argument('--scope',default='')
    args=p.parse_args();settings_and_environment()
    with db.session_scope() as session:
        generation=repo.lock_user_state(session,args.user_id,scope=args.scope,now=db.utc_naive())
        jobs=session.scalars(select(MemoryJobRow).where(MemoryJobRow.user_id==args.user_id,MemoryJobRow.scope==args.scope,MemoryJobRow.kind=='graph_extract')).all()
        for job in jobs:
            job.status='failed';job.last_error='generic_graph_retired';job.payload={};job.stages={};job.claim_token='';job.finished_at=db.utc_naive()
        runtime.purge(session,user_id=args.user_id,scope=args.scope,generation=generation,now=db.utc_naive())
    try:
        runtime.sync(user_id=args.user_id,scope=args.scope)
        print(json.dumps({'withdrawn':True,'cancelled_legacy_jobs':len(jobs)}));return 0
    except Exception as exc:
        print(json.dumps({'withdrawn':False,'cleanup_intent_persisted':True,'error_type':type(exc).__name__}));return 1

if __name__=='__main__':raise SystemExit(main())
