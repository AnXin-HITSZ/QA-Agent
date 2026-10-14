"""Import retained local indexing audit into migrated MySQL; never reruns extraction."""
import argparse
import json
import os
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--backfill-active",action="store_true",
                        help="Persist verified legacy extraction policies for the active generation; no reindexing")
    args=parser.parse_args()
    os.chdir(BACKEND)
    from app.rag import job_repo, ocr_jobs
    if not job_repo.configured():
        parser.error("MYSQL_URL is required")
    if ocr_jobs.job_running():
        parser.error("Wait for the current indexing job before importing")
    paths=list(ocr_jobs.jobs_dir().glob("*.json"))
    if not args.apply:
        print(json.dumps({"retained_legacy_files":len(paths),"applied":False}))
        return
    imported=job_repo.import_legacy(ocr_jobs.jobs_dir())
    filled=0
    if args.backfill_active:
        from app.rag import rebuild, store
        active=store.active_collection()
        existing=job_repo.policies(active)
        verified=rebuild.legacy_policies(active)
        additions=[row for key,row in verified.items() if key not in existing and rebuild.options_of(row)]
        if active != store.active_collection() or ocr_jobs.job_running():
            raise RuntimeError("Index changed during backfill; retry when indexing is idle")
        job_repo.save_policies(active, additions)
        filled=len(additions)
    print(json.dumps({"imported_jobs":imported,"total_jobs":job_repo.list_page(0,1)["total"],
                      "legacy_files_preserved":True,"backfilled_version_files":filled}))

if __name__=="__main__":
    main()
