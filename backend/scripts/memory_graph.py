"""Graph administration: explicit actions, no paid calls for health/status/audit."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.memory import db, repo
from app.memory.graph import get_graph_client, runtime


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("health", "status", "build", "rebuild", "reconcile", "repair", "retry", "resolve", "purge-run"))
    p.add_argument("--user-id")
    p.add_argument("--scope", default="")
    p.add_argument("--run-id")
    p.add_argument("--mention-id", type=int)
    p.add_argument("--entity-id")
    args = p.parse_args()
    client = get_graph_client()
    try:
        if args.action == "health":
            client.connect()
            print(json.dumps(client.health(), ensure_ascii=False))
            return 0 if client.health()["available"] else 1
        if args.action == "purge-run":
            if not args.run_id:
                p.error("purge-run requires --run-id")
            from scripts.memory_eval import EvalEnv, purge
            result = purge(EvalEnv(args.run_id))
            print(json.dumps(result, ensure_ascii=False, default=str))
            return 0 if result["clean"] else 1
        if not args.user_id:
            p.error("this action requires --user-id")
        with db.session_scope() as session:
            data = runtime.snapshot(session, user_id=args.user_id, scope=args.scope)
            if args.action == "resolve":
                from app.memory.graph.tables import MemoryEntityMentionRow, MemoryEntityRow
                from app.memory.graph import repo as graph_repo
                repo.lock_user_state(session, args.user_id, scope=args.scope, now=db.utc_naive())
                data = runtime.snapshot(session, user_id=args.user_id, scope=args.scope)
                mention = session.get(MemoryEntityMentionRow, args.mention_id)
                entity = session.get(MemoryEntityRow, args.entity_id)
                if not mention or not entity or any((r.user_id, r.scope, r.generation) !=
                    (args.user_id, args.scope, data["generation"]) for r in (mention, entity)):
                    raise ValueError("消歧对象越界或代次不一致")
                graph_repo.resolve_mention(session, mention_id=mention.id, entity_id=entity.id,
                    status="resolved", evidence={"decision": "operator_confirmed"})
                item = repo.get_item(session, args.user_id, mention.memory_id)
                if item:
                    runtime.enqueue(session, item, now=db.utc_naive(), force=True)
                runtime.queue_sync(session, user_id=args.user_id, scope=args.scope,
                    generation=data["generation"], now=db.utc_naive())
                print('{"resolved":true}')
                return 0
            if args.action == "build":
                if not runtime.switches().write:
                    raise ValueError("图写入未启用")
                after = ""
                queued = 0
                while True:
                    items = repo.index_items_page(session, user_id=args.user_id, scope=args.scope, after=after, limit=100)
                    if not items:
                        break
                    for item in items:
                        if runtime.enqueue(session, item, now=db.utc_naive()):
                            queued += 1
                    after = items[-1].id
                print(json.dumps({"queued_items": queued, "note": "由 Worker 执行，包含付费图提取"}))
                return 0
            if args.action == "retry":
                from sqlalchemy import select, update
                from app.memory.tables import MemoryJobRow, MemoryOpRow
                jobs = session.scalars(select(MemoryJobRow).where(
                    MemoryJobRow.user_id == args.user_id, MemoryJobRow.scope == args.scope,
                    MemoryJobRow.kind == runtime.JOB_GRAPH, MemoryJobRow.status == "failed")).all()
                for job in jobs:
                    job.payload = runtime.retry_input(session, job)
                    job.status, job.attempts = "pending", 0
                    job.next_run_at, job.finished_at = db.utc_naive(), None
                    job.last_error = ""
                session.execute(update(MemoryOpRow).where(MemoryOpRow.user_id == args.user_id,
                    MemoryOpRow.scope == args.scope, MemoryOpRow.kind == runtime.OP_GRAPH,
                    MemoryOpRow.status == "failed").values(status="pending", attempts=0,
                    next_run_at=db.utc_naive(), last_error="", finished_at=None))
                print('{"retry_registered":true}')
                return 0
        if args.action == "status":
            from sqlalchemy import select, func
            from app.memory.graph.tables import MemoryEntityMentionRow
            from app.memory.tables import MemoryJobRow
            with db.session_scope() as session:
                mentions = dict(session.execute(select(MemoryEntityMentionRow.status, func.count())
                    .where(MemoryEntityMentionRow.user_id == args.user_id,
                           MemoryEntityMentionRow.scope == args.scope)
                    .group_by(MemoryEntityMentionRow.status)).all())
                failures = [{"job_id": row.id, "thread_id": row.thread_id,
                             "last_error": row.last_error,
                             "diagnostics": (row.stages or {}).get("graph_diagnostics", {})}
                    for row in session.scalars(select(MemoryJobRow).where(
                        MemoryJobRow.user_id == args.user_id, MemoryJobRow.scope == args.scope,
                        MemoryJobRow.kind == runtime.JOB_GRAPH, MemoryJobRow.status == "failed")
                        .order_by(MemoryJobRow.id).limit(20))]
            print(json.dumps({"digest": data["digest"], "counts": {k: len(data[k]) for k in
                ("entities", "relations", "events", "links", "mentions")},
                "mention_status": mentions, "failed_graph_jobs": failures}, ensure_ascii=False))
            return 0
        if not runtime.switches().master or not client.connect():
            raise RuntimeError("图关闭或 Neo4j 不可用")
        if args.action in ("rebuild", "repair"):
            runtime.sync(user_id=args.user_id, scope=args.scope)
        result = client.audit(data)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["clean"] else 1
    except Exception as exc:
        print("图管理操作未完成：" + type(exc).__name__, file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
