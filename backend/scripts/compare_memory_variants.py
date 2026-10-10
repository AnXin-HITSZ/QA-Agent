"""Compare named variants only when build, questions and configurations match."""
import argparse
import json
from pathlib import Path


def compare(root: Path, names: list[str]) -> dict:
    runs = []
    for name in names:
        if not name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in name) or name in (".", ".."):
            raise ValueError("invalid variant name")
        path = root / "variants" / name
        manifest = json.loads((path / "run.json").read_text(encoding="utf-8"))
        results = json.loads((path / "results.json").read_text(encoding="utf-8"))
        scores = json.loads((path / "scores.json").read_text(encoding="utf-8"))
        from scripts.evalkit.scoring import results_digest
        if scores.get("results_digest") != results_digest(results):
            raise ValueError("评分不属于当前答案，请重新评分")
        if not results.get("run_valid"):
            raise ValueError("invalid variant results")
        signature = {k: manifest.get(k) for k in ("code_commit", "source_digest", "dataset", "scope", "collection",
            "llm_model", "llm_temperature", "embeddings_model", "embeddings_dim", "rerank_model", "rerank_enabled",
            "memory_top_k", "memory_vector_k", "memory_bm25_k", "memory_rerank_k", "memory_rrf_k", "memory_context_chars",
            "answer_instruction", "context_chars")}
        signature["dataset"] = manifest.get("dataset", {}).get("digest")
        signature["questions"] = [(q["id"], q["question"]) for q in results["questions"]]
        signature["modes"] = manifest.get("modes")
        signature["scoring_protocol"] = scores.get("protocol")
        graph = dict(manifest.get("graph", {}))
        graph["switches"] = {k: v for k, v in graph.get("switches", {}).items() if k != "search"}
        signature["graph"] = graph
        if runs and signature != runs[0]["signature"]:
            raise ValueError("变体条件不同，不能归因于图召回")
        if not manifest.get("source_digest"):
            raise ValueError("变体缺少代码摘要，不能验证固定代码")
        runs.append({"variant": name, "signature": signature,
                     "graph_search": manifest.get("graph", {}).get("switches", {}).get("search"),
                     "summary": scores["summary"]["by_mode"]})
    result = {"comparable": True, "variants": [{k: v for k, v in r.items() if k != "signature"} for r in runs],
              "note": "评分模型/规则仍需相同；统计改善不等于已证明泛化收益。"}
    (root / "variant-comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 固定构建消融对比", "", "| 变体 | 图召回 | 模式 | F1 | Judge |", "|---|---|---|---|---|"]
    for r in runs:
        for mode, values in r["summary"].items():
            lines.append(f"| {r['variant']} | {r['graph_search']} | {mode} | {values.get('mean_f1')} | {values.get('judge_accuracy')} |")
    (root / "variant-comparison.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--variants", required=True)
    args = p.parse_args()
    from memory_eval import check_run_id, RUNS_DIR
    print(json.dumps(compare(RUNS_DIR / check_run_id(args.run_id), args.variants.split(",")), ensure_ascii=False))


if __name__ == "__main__":
    main()
