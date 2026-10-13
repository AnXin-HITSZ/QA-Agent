"""Collect actual knowledge-tool contexts in the serving environment, then score
saved records with Ragas in a separate environment. Live commands are opt-in.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlparse
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
DEFAULT_METRICS = ("context_precision", "context_recall", "faithfulness", "factual_correctness")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def read_cases(path: Path) -> list[dict]:
    data = read_json(path)
    cases = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(cases, list) or not cases:
        raise ValueError("评测集必须包含非空 cases 列表")
    seen = set()
    for row in cases:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip()
                                               for k in ("id", "user_input", "reference")):
            raise ValueError("每项必须有非空 id/user_input/reference")
        if row["id"] in seen:
            raise ValueError("评测项 id 不能重复")
        if not isinstance(row.get("answerable", True), bool):
            raise ValueError("answerable 必须为布尔值")
        if not isinstance(row.get("expected_sources", []), list) or any(not isinstance(k, str) or not k.strip() for k in row.get("expected_sources", [])):
            raise ValueError("expected_sources 必须为列表")
        seen.add(row["id"])
    return cases


def require_review(data, command):
    if command != "synthetic" and (not isinstance(data, dict) or data.get("review_status") != "approved"):
        raise ValueError("真实评测集需先人工确认，并标记 review_status=approved")
    if command == "synthetic" and not data.get("synthetic"):
        raise ValueError("synthetic 入口只接受明确标记的合成集")


def contexts_from_messages(messages: list) -> tuple[list[str], list[dict], list[str], bool]:
    """Use what the model actually read, not another retrieval or untrimmed artifacts.

    Contexts are per formatted evidence block. SOP/other tool use is flagged so
    missing auxiliary evidence cannot silently inflate/deflate RAG-only metrics.
    """
    from langchain_core.messages import ToolMessage
    contexts, sources, tools = [], [], []
    retrieval_error = False
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        tools.append(message.name or "unknown")
        if message.name != "search_knowledge":
            continue
        text = message.content if isinstance(message.content, str) else ""
        if "知识库当前不可用" in text or "知识库暂时无法访问" in text:
            retrieval_error = True
        # Empty artifacts mean no evidence: don't call a no-hit notice a context.
        if not message.artifact:
            continue
        starts = [m.start() for m in re.finditer(r"(?m)^\[\d+\] 来源:", text)]
        if starts:
            for start, end in zip(starts, starts[1:] + [len(text)]):
                block = text[start:end].strip()
                if block:
                    contexts.append(block)
        elif text.strip():
            contexts.append(text.strip())
        for hit in message.artifact:
            sources.append({k: hit.get(k) for k in ("oss_key", "page_numbers", "section_path", "chunk_indices", "score", "truncated")})
    return contexts, sources, tools, retrieval_error


def versions() -> dict:
    result = {}
    for name in ("ragas", "langchain-core", "langchain-openai", "langgraph", "qdrant-client"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def collect(cases: list[dict], out: Path, *, collection: str, synthetic: bool):
    from app.config import get_settings
    from app.rag import store
    from app.graph.builder import build_graph
    from langchain_core.messages import HumanMessage
    s = get_settings()
    if not store.collection_exists(collection):
        raise ValueError("指定索引不存在；评测不会创建或重建当前知识库")
    # Pin all retrieval calls to the exact collection chosen for this run.
    store.active_collection = lambda: collection
    dataset_path = out / "dataset.json"
    write_json(dataset_path, {"synthetic": synthetic, "cases": cases})
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2], text=True).strip()
    except Exception:
        commit = "unavailable"
    settings = {k: v for k, v in s.model_dump().items() if k.startswith("rag_")}
    from app.graph.nodes import SYSTEM_PROMPT
    write_json(out / "manifest.json", {"synthetic": synthetic, "collection": collection,
        "points": store.collection_count(collection), "commit": commit, "dependencies": versions(),
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "rag_settings": settings, "generation_model": s.llm_model, "generation_temperature": s.llm_temperature,
        "generation_endpoint_host": urlparse(s.llm_base_url).hostname,
        "evaluation_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "mode": "actual_react_graph_no_user_memory_todos_or_authenticated_actions"})
    graph = build_graph(checkpointer=None)
    async def run():
        records = []
        path = out / "records.jsonl"
        with path.open("x", encoding="utf-8") as stream:
            for case in cases:
                started = time.monotonic()
                row = dict(case)
                try:
                    result = await graph.ainvoke({"messages": [HumanMessage(content=case["user_input"])]},
                        config={"recursion_limit": 12, "configurable": {"memory_prompt": "", "todos_prompt": ""}})
                    final = result["messages"][-1]
                    if not isinstance(final.content, str) or getattr(final, "tool_calls", None):
                        raise ValueError("No final text answer")
                    contexts, sources, tools, error = contexts_from_messages(result["messages"])
                    row.update(response=final.content, retrieved_contexts=contexts, sources=sources,
                        tools=tools, retrieval_called="search_knowledge" in tools,
                        auxiliary_tools_used=[t for t in tools if t not in ("search_knowledge", "list_sops")],
                        collection_status="retrieval_error" if error else "ok")
                    expected = set(case.get("expected_sources", []))
                    found = {source.get("oss_key") for source in sources}
                    row["expected_source_recall"] = len(expected & found) / len(expected) if expected else None
                except Exception as exc:
                    row.update(collection_status="failed", error_type=type(exc).__name__)
                row["total_latency_ms"] = round((time.monotonic() - started) * 1000, 2)
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                records.append(row)
                print(json.dumps({"collected": case["id"], "status": row["collection_status"]}), flush=True)
        return records
    return asyncio.run(run())


def aggregate(rows: list[dict], metrics: list[str]) -> dict:
    total = len(rows)
    summaries = {}
    for name in metrics:
        values = [r["metrics"][name]["value"] for r in rows if r["metrics"][name]["status"] == "ok"]
        summaries[name] = {"mean": sum(values) / len(values) if values else None,
                           "valid": len(values), "total": total,
                           "failed": sum(r["metrics"][name]["status"] == "failed" for r in rows),
                           "not_applicable": sum(r["metrics"][name]["status"] == "not_applicable" for r in rows)}
    return {"total": total, "metrics": summaries}


def score(records_path: Path, out: Path, metric_names: list[str], timeout: int, retry_failed: bool = False):
    from ragas import EvaluationDataset, evaluate
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, LLMContextRecall, LLMContextPrecisionWithReference, FactualCorrectness
    from ragas.run_config import RunConfig
    from langchain_openai import ChatOpenAI
    from dotenv import dotenv_values
    from app.config import get_settings
    s = get_settings()
    env = dotenv_values(".env")
    def setting(key, fallback):
        return os.environ.get(key) or env.get(key) or fallback
    model = setting("RAGAS_JUDGE_MODEL", s.llm_model)
    url = setting("RAGAS_JUDGE_BASE_URL", s.llm_base_url)
    key = setting("RAGAS_JUDGE_API_KEY", s.llm_api_key)
    judge = LangchainLLMWrapper(ChatOpenAI(model=model, base_url=url, api_key=key, temperature=0,
                                          timeout=timeout, max_retries=1))
    factories = {"faithfulness": Faithfulness, "context_recall": LLMContextRecall,
                 "context_precision": LLMContextPrecisionWithReference, "factual_correctness": FactualCorrectness}
    records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [r.get("id") for r in records]
    if len(ids) != len(set(ids)) or any(not isinstance(i, str) or not i for i in ids):
        raise ValueError("评分输入必须有非重复的题目 id")
    path = out / "scores.jsonl"
    saved = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    existing = {row["id"]: row for row in saved}
    manifest_path = out / "judge.json"
    manifest = {"records_sha256": hashlib.sha256(records_path.read_bytes()).hexdigest(),
                "model": model, "endpoint_host": urlparse(url).hostname, "metrics": metric_names, "dependencies": versions()}
    if manifest_path.exists() and read_json(manifest_path) != manifest:
        raise ValueError("裁判模型、依赖、指标或输入变化；请使用新的评分目录")
    write_json(manifest_path, manifest)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            previous = existing.get(record["id"])
            if previous and (not retry_failed or not any(m["status"] == "failed" for m in previous["metrics"].values())):
                continue
            row = {"id": record["id"], "category": record.get("category", "unspecified"),
                   "metrics": dict(previous["metrics"]) if previous else {}}
            for name in metric_names:
                if row["metrics"].get(name, {}).get("status") in ("ok", "not_applicable"):
                    continue
                if record.get("collection_status") != "ok":
                    item = {"value": None, "status": "failed", "reason": "collection_failed"}
                elif record.get("auxiliary_tools_used"):
                    item = {"value": None, "status": "not_applicable", "reason": "auxiliary_evidence_not_scored_as_rag"}
                elif not record.get("answerable", True):
                    item = {"value": None, "status": "not_applicable", "reason": "negative_case_requires_separate_refusal_review"}
                else:
                    try:
                        sample = {k: record[k] for k in ("user_input", "response", "retrieved_contexts", "reference")}
                        result = evaluate(EvaluationDataset.from_list([sample]), metrics=[factories[name]()], llm=judge,
                            run_config=RunConfig(timeout=timeout, max_retries=1, max_workers=1),
                            raise_exceptions=True, show_progress=False)
                        value = float(next(iter(result.scores[0].values())))
                        if not math.isfinite(value) or not 0 <= value <= 1:
                            raise ValueError("Undefined or invalid metric")
                        item = {"value": value, "status": "ok"}
                    except Exception as exc:
                        item = {"value": None, "status": "failed", "error_type": type(exc).__name__}
                row["metrics"][name] = item
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            existing[row["id"]] = row
            print(json.dumps({"scored": record["id"], "metrics": row["metrics"]}), flush=True)
    summary = aggregate(list(existing.values()), metric_names)
    source_scores = [r["expected_source_recall"] for r in records if r.get("expected_source_recall") is not None]
    summary.update(judge_model=model, judge_matches_generator=model == s.llm_model,
        expected_source_recall={"mean": sum(source_scores) / len(source_scores) if source_scores else None, "valid": len(source_scores)},
        retrieval_called=sum(bool(r.get("retrieval_called")) for r in records),
        context_present=sum(bool(r.get("retrieved_contexts")) for r in records),
        collection_failed=sum(r.get("collection_status") != "ok" for r in records))
    manifest_file = records_path.parent / "manifest.json"
    summary["synthetic"] = read_json(manifest_file).get("synthetic") if manifest_file.exists() else None
    summary["limitations"] = ["Small/synthetic sets do not estimate production corpus quality",
        "LLM judge scores are estimates; same generator/judge model may bias results",
        "Failed/undefined metrics are excluded from means and reported with coverage",
        "No memory, todos, authenticated business actions or HTTP/auth assessment",
        "Negative/unanswerable questions require separate refusal evaluation"]
    write_json(out / "report.json", summary)
    text = "# RAG Ragas 评测\n\n" + ("合成样本流程验证，不代表真实知识库质量。\n\n" if summary["synthetic"] else "请结合样本覆盖率与人工复核解读。\n\n")
    text += "| 指标 | 均值 | 有效/总数 | 失败 | 不适用 |\n|---|---:|---:|---:|---:|\n"
    for name, stats in summary["metrics"].items():
        value = f"{stats['mean']:.4f}" if stats["mean"] is not None else "无有效评分"
        text += f"| {name} | {value} | {stats['valid']}/{stats['total']} | {stats['failed']} | {stats['not_applicable']} |\n"
    text += f"\n裁判：{model}。检索调用 {summary['retrieval_called']}/{summary['total']}，有上下文 {summary['context_present']}/{summary['total']}。\n"
    (out / "report.md").write_text(text, encoding="utf-8")
    return summary


def synthetic_run(dataset: Path, out: Path, limit: int):
    """Real embeddings/Qdrant with memory-only synthetic source and cache."""
    from app.config import get_settings
    from app.rag import cache_store, ingest, store
    from app.skills import loader
    s = get_settings()
    base = s.qdrant_collection + "_ragas_" + uuid.uuid4().hex[:12]
    s.qdrant_collection = base
    s.index_state_dir = str(out / "index")
    s.metering_enabled = False
    cache_store.install(cache_store.MemoryCache())
    data = read_json(dataset)
    synthetic = bool(data.get("synthetic", False))
    if synthetic:
        files = {key: text.encode() for key, text in data["documents"].items()}
    else:
        files = {}
        for source in data["document_sources"]:
            raw = (dataset.parent / source["local_path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != source["content_sha256"]:
                raise ValueError("原件快照哈希与确认的评测集不一致")
            files[source["key"]] = raw
    class EvaluationSource:
        def list_all(self, prefix=""):
            return [{"key": key, "size": len(value), "last_modified": 0}
                    for key, value in files.items() if key.startswith(prefix)]
        def get_object(self, key):
            return files[key]
        def object_exists(self, key):
            return key in files
    kb = EvaluationSource()
    loader.get_catalog = lambda: []
    loader.get_skill = lambda key: None
    try:
        result = ingest.run_index_job(kb, ingest.Scope(), ingest.Options())
        if not result["published"]:
            raise ValueError("合成资料索引失败")
        records = collect(read_cases(dataset)[:limit], out, collection=store.active_collection(), synthetic=synthetic)
        manifest = read_json(out / "manifest.json")
        manifest.update(corpus_scope="specified_snapshot", document_hashes={key: hashlib.sha256(raw).hexdigest() for key, raw in files.items()})
        write_json(out / "manifest.json", manifest)
        return records
    finally:
        for name in [c.name for c in store.get_client().get_collections().collections]:
            if name == base or name.startswith(base + "__"):
                store.get_client().delete_collection(name)
        write_json(out / "cleanup.json", {"collection_prefix": base, "removed": True})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "synthetic", "isolated"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--dataset", type=Path, required=True)
        cmd.add_argument("--out", type=Path, required=True)
        cmd.add_argument("--limit", type=int, default=5)
        cmd.add_argument("--allow-paid", action="store_true")
    cmd = sub.add_parser("score")
    cmd.add_argument("--records", type=Path, required=True)
    cmd.add_argument("--out", type=Path, required=True)
    cmd.add_argument("--metrics", default=",".join(DEFAULT_METRICS))
    cmd.add_argument("--timeout", type=int, default=120)
    cmd.add_argument("--retry-failed", action="store_true", help="只重试失败指标，保留已成功评分")
    cmd.add_argument("--allow-paid", action="store_true")
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error("真实生成/检索/裁判调用需要 --allow-paid")
    os.environ["RAGAS_DO_NOT_TRACK"] = "true"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    logging.disable(logging.CRITICAL)
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    from app.config import get_settings
    s = get_settings()
    os.environ["NO_PROXY"] = ",".join(filter(None, [urlparse(s.qdrant_url).hostname, "localhost", "127.0.0.1", os.environ.get("NO_PROXY")]))
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    try:
        if args.command == "score":
            names = args.metrics.split(",")
            if any(n not in DEFAULT_METRICS for n in names) or len(names) != len(set(names)):
                raise ValueError("指标必须是四个默认指标的非重复子集")
            args.out.mkdir(parents=True, exist_ok=True)
            score(args.records, args.out, names, args.timeout, args.retry_failed)
        else:
            if args.limit < 1:
                raise ValueError("limit 必须为正")
            read_cases(args.dataset)  # Validate before any paid call.
            data = read_json(args.dataset)
            require_review(data, args.command)
            args.out.mkdir(parents=True, exist_ok=False)
            if args.command in ("synthetic", "isolated"):
                synthetic_run(args.dataset, args.out, args.limit)
            else:
                from app.rag import store
                collect(read_cases(args.dataset)[:args.limit], args.out, collection=store.active_collection(), synthetic=False)
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "output": str(args.out.resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
