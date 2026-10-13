"""Administrative projection management; no LLM extraction or generic recall."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.run_memory_eval import settings_and_environment
from app.matters import projection
from app.matters.tables import Projection
from app.memory import db
from sqlalchemy import select

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['health','init','status','sync','rebuild','audit'])
    p.add_argument('--user-id',default='');p.add_argument('--matter-id',default='')
    args=p.parse_args();settings=settings_and_environment()
    try:
        if args.action=='init':
            projection.initialize();print(json.dumps({'initialized':True}));return 0
        if args.action=='health':
            client=projection.driver()
            if client is None:print(json.dumps({'enabled':False,'available':False}));return 0
            client.verify_connectivity();print(json.dumps({'enabled':True,'available':True}));return 0
        if not args.user_id:raise ValueError('需要指定 user-id，禁止跨用户批量操作')
        if args.action=='status':
            with db.session_scope() as session:
                q=select(Projection).where(Projection.user_id==args.user_id)
                if args.matter_id:q=q.where(Projection.id==args.matter_id)
                rows=[{'id':x.id,'revision':x.revision,'published_revision':x.published_revision,'attempts':x.attempts,'error':x.last_error} for x in session.scalars(q.limit(200))]
            print(json.dumps({'items':rows}));return 0
        if not args.matter_id:raise ValueError('需要指定 matter-id，逐事务执行')
        if args.action in ('sync','rebuild'):
            clean=projection.sync_one(args.user_id,args.matter_id)
            print(json.dumps({'synced':clean}));return 0 if clean else 1
        data=projection.snapshot(args.user_id,args.matter_id)
        if not data:raise ValueError('事项不存在')
        clean=projection.audit(data)
        print(json.dumps({'clean':clean,'authoritative_revision':data['revision']}));return 0 if clean else 1
    except Exception as exc:
        print('操作未完成：'+type(exc).__name__,file=sys.stderr);return 1
    finally:projection.stop()

if __name__=='__main__':raise SystemExit(main())
