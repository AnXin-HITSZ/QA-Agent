"""Import historical user sources into an existing isolated evaluation build. No model calls."""
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import text
from app.memory import db, episodes, vector
from app.config import get_settings
from scripts.memory_eval import EvalEnv, exchanges, load_dataset, eval_user_id


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    args=p.parse_args()
    if get_settings().memory_source_retention_days <= 0:
        raise SystemExit("set MEMORY_SOURCE_RETENTION_DAYS to a positive value before importing authorized source data")
    env=EvalEnv(args.run_id)
    if env.path("purge.json").exists():
        raise SystemExit("purged build cannot be restored")
    build=json.loads(env.path("build.json").read_text(encoding="utf-8"))
    if not build.get("complete") or env.collection != "user_memory_eval_dev":
        raise SystemExit("requires complete build and dedicated development collection")
    dataset=load_dataset(str(env.path("dataset.json")))
    if dataset.get("schema_version") != 2:
        raise SystemExit("source import currently requires v2 historical sessions")
    if dataset["_digest"] != build["dataset"]["digest"]:
        raise SystemExit("dataset digest mismatch")
    # Only historical exchanges are passed to retention; QA/evidence never enters storage.
    snapshot=json.loads(env.path("memory_snapshot.json").read_text(encoding="utf-8"))
    generations={}
    for fact in snapshot:
        generations.setdefault(fact["user_id"],set()).add(fact["generation"])
    count=0
    links=0
    for unit in exchanges(dataset):
        uid=eval_user_id(env.run_id,unit["subject"])
        versions=generations.get(uid,set())
        if len(versions)!=1:
            raise SystemExit("missing or inconsistent generation snapshot")
        with db.session_scope() as session:
            if session.execute(text("SELECT DATABASE()")).scalar() != "qa_agent_dev":
                raise SystemExit("development database required")
            links += episodes.repair_source_links(session, user_id=uid, scope=env.scope,
                turn_id=unit["turn_id"], now=db.utc_naive(), expected_generation=next(iter(versions)))
            count += bool(episodes.retain(session, user_id=uid, expected_generation=next(iter(versions)),
                scope=env.scope, turn_id=unit["turn_id"], messages=unit["messages"], now=db.utc_naive()))
    stamp=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    env.write(f"source-imports/{stamp}.json", {"protocol": episodes.PROTOCOL, "episodes_imported":count,
        "source_links_repaired":links, "dataset_digest":dataset["_digest"], "scope":env.scope, "history_only":True})
    print(json.dumps({"episodes_imported":count,"source_links_repaired":links,"scope":env.scope}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
