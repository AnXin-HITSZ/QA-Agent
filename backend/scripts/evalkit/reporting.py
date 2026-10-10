"""把一次评测的产物汇总成 report.json 与 report.md(只读文件,不连库、不调模型)。

产物来源(有哪份读哪份,缺的如实标注):
- `dataset.json` / `build.json` / `results.json` / `run.json` / `scores.json` / `purge.json`,
  都在 `data/eval/runs/<run-id>/` 下(见 memory_eval.py)。

两条纪律写进代码:
- 失败与跳过**不进平均值**,也不记 0(汇总口径见 evalkit.scoring.aggregate);
- 报告不宣称复现官方 / 论文成绩:延迟与 token 一律带口径说明,缺用量记 unknown。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

LIMITS = [
    "F1 与官方 evaluation.py 对齐口径(独立实现),但**不做词干化**:分数不可与官方结果直接对比;"
    "BLEU-1 是本仓库补充指标,官方没有该指标。",
    "上下文用字符数记录,不折算 token(离线无法准确 token 化);token 用量只统计模型响应实际"
    "返回的 usage,缺失一律记 unknown,不记 0。",
    "计量库口径的 usage 按时间窗 + 用途汇总,可能混入同一窗口内其他用户的调用,也不包含回答与"
    "裁判调用 —— 不能当作本次运行的完整费用。",
    "memory 模式导出的是检索证据(诊断用);第一版不计算标准 evidence 的召回率,也不把"
    " memory_id 与 dia_id 直接比较。",
    "本报告是离线可复现的评测产物,不构成对官方基准或 Mem0 论文成绩的复现 / 对比声明。",
]


def percentile(values: list[float], p: float) -> float | None:
    """nearest-rank 分位数(第 ⌈p·n⌉ 个);样本为空返回 None。方法名写进报告。"""
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    k = max(1, math.ceil(p / 100 * len(ordered)))
    return round(ordered[k - 1], 1)


def latency_stats(values: list[float]) -> dict | None:
    """一组延迟(毫秒)的 n / p50 / p95 / max;有效样本数与统计方法一并给出。"""
    if not values:
        return None
    return {"n": len(values), "method": "nearest-rank",
            "p50": percentile(values, 50), "p95": percentile(values, 95),
            "max": round(max(float(v) for v in values), 1)}


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def build_report(run_dir: Path) -> dict:
    """汇总 run 目录下的产物(不写文件);results.json 缺失时抛 FileNotFoundError。"""
    results = _load(run_dir / "results.json")
    if results is None:
        raise FileNotFoundError(f"{run_dir / 'results.json'} 不存在:先跑 build/ask(或 run)")
    scores = _load(run_dir / "scores.json")
    if scores:
        from .scoring import results_digest
        if scores.get("results_digest") != results_digest(results):
            raise ValueError("scores.json 与当前 results.json 不匹配，请重新 score")
        if results.get("run_valid") is False:
            raise ValueError("无效构建不能汇总正常质量评分")
    build_dir = run_dir.parent.parent if run_dir.parent.name == "variants" else run_dir
    build = _load(build_dir / "build.json")
    dataset = _load(build_dir / "dataset.json")
    run_manifest = _load(run_dir / "run.json")
    purge = _load(build_dir / "purge.json")

    questions = results.get("questions") or []
    modes: list[str] = []
    for row in questions:
        for mode in (row.get("modes") or {}):
            if mode not in modes:
                modes.append(mode)

    # 逐模式的延迟(只统计有数值的记录;失败/跳过的题不记 0 也不计入)
    latency: dict[str, dict] = {}
    for mode in modes:
        answers, retrievals, totals = [], [], []
        for row in questions:
            entry = (row.get("modes") or {}).get(mode)
            if not isinstance(entry, dict) or entry.get("skipped") or entry.get("answer_error") or entry.get("error"):
                continue
            if isinstance(entry.get("total_latency_ms"), (int, float)):
                totals.append(float(entry["total_latency_ms"]))
            if isinstance(entry.get("answer_latency_ms"), (int, float)):
                answers.append(float(entry["answer_latency_ms"]))
            if isinstance(entry.get("retrieval_latency_ms"), (int, float)):
                retrievals.append(float(entry["retrieval_latency_ms"]))
        latency[mode] = {"answer": latency_stats(answers), "retrieval": latency_stats(retrievals),
                         "total": latency_stats(totals)}

    failures = []
    for row in questions:
        for mode, entry in (row.get("modes") or {}).items():
            if not isinstance(entry, dict):
                continue
            for key, label in (("error", "检索/模式错误"), ("answer_error", "回答调用/模式执行失败")):
                if entry.get(key):
                    failures.append({"id": row.get("id"), "conversation_id": row.get("conversation_id", ""),
                                     "mode": mode, "kind": label, "error": str(entry[key])[:200]})

    evidence = {"scored_questions": len(questions), "with_evidence": 0, "evidence_refs": 0,
                "unresolved_refs": 0, "status_counts": {}}
    for row in questions:
        expected = row.get("evidence_expected") or []
        unresolved = row.get("evidence_unresolved") or []
        status = str(row.get("evidence_status") or "")
        if expected:
            evidence["with_evidence"] += 1
        evidence["evidence_refs"] += len(expected)
        evidence["unresolved_refs"] += len(unresolved)
        if status:
            evidence["status_counts"][status] = evidence["status_counts"].get(status, 0) + 1
    evidence["note"] = ("仅导出标准 evidence 的数量用于人工诊断;第一版不计算压缩记忆的证据"
                        "召回率,也不把 memory_id 与 dia_id 直接比较。")

    report = {
        "run_id": results.get("run_id", ""),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_valid": bool(results.get("run_valid", True)),
        "invalid_reason": results.get("invalid_reason", ""),
        "answer_calls": bool(results.get("answer_calls")),
        "dataset": ({"name": dataset.get("name", ""), "digest": dataset.get("_digest", ""),
                     "schema_version": int(dataset.get("schema_version") or 1),
                     "conversations": len(dataset.get("conversations") or []),
                     "questions": len(dataset.get("questions") or [])} if dataset else None),
        "build": ({"complete": bool(build.get("complete")), "drained": build.get("drained"),
                   "ticks": build.get("ticks"), "memories": build.get("memories"),
                   "jobs_failed": (build.get("jobs") or {}).get("failed", 0),
                   "index_pending": build.get("index_pending", 0),
                   "cleanup": build.get("cleanup"), "last_error": build.get("last_error", "")}
                  if build else None),
        "manifest": ({"code_commit": run_manifest.get("code_commit", ""),
                      "worktree_dirty": run_manifest.get("worktree_dirty"),
                      "llm_model": run_manifest.get("llm_model", ""),
                      "answer_instruction_version": run_manifest.get("answer_instruction_version", "")}
                     if run_manifest else None),
        "modes": modes,
        "summary": (scores.get("summary") if scores else None),
        "protocol": (scores.get("protocol") if scores else None),
        "latency": latency,
        "tokens": results.get("llm_usage"),
        "failures": failures,
        "evidence_diagnostics": evidence,
        "purge": ({"clean": purge.get("clean"), "points_left": purge.get("points_left"),
                   "users": len(purge.get("users") or []), "errors": len(purge.get("errors") or [])}
                  if purge else None),
        "limits": list(LIMITS) + ([] if scores else ["尚未运行 score:本报告不含评分汇总。"]),
    }
    # 人工复核单独展示；不修改参考答案、评分、分母或实验输入。
    review_path = run_dir / "annotations.json"
    if review_path.exists():
        review = json.loads(review_path.read_text(encoding="utf-8"))
        known = {q["id"] for q in results.get("questions", [])}
        if not isinstance(review, dict) or review.get("run_id") != report["run_id"]:
            raise ValueError("annotations.json 的 run_id 不匹配")
        entries = review.get("entries")
        if not isinstance(entries, list) or any(
                not isinstance(e, dict) or e.get("question_id") not in known
                or e.get("kind") not in ("reference_conflict", "metric_mismatch", "manual_review")
                or not isinstance(e.get("note"), str) for e in entries):
            raise ValueError("annotations.json 的题号、类型或说明不合法")
        report["annotations"] = entries
    return report


def render_markdown(report: dict) -> str:
    """report.json 的同内容可读版(中文,表格化)。"""
    lines: list[str] = [f"# 长期记忆评测报告 · run `{report['run_id']}`", ""]
    if not report["run_valid"]:
        lines += [f"> **结果无效**:{report.get('invalid_reason') or '构建未完成或未通过有效检查'}"
                  "(如诊断性放行),不得计入正常质量对比。", ""]
    lines += [f"- 生成时间:{report['generated_at']}",
              f"- 是否生成答案:{'是' if report['answer_calls'] else '否(仅召回与导出)'}"]
    if report["dataset"]:
        d = report["dataset"]
        lines.append(f"- 数据集:{d['name']}(schema v{d['schema_version']},"
                     f"{d['conversations']} 段会话 / {d['questions']} 条问题,摘要 {d['digest']})")
    if report["manifest"]:
        m = report["manifest"]
        lines.append(f"- 模型:{m['llm_model']};代码版本:{m['code_commit'][:12]}"
                     f"{'(工作区有未提交改动)' if m.get('worktree_dirty') else ''};"
                     f"回答指令:{m.get('answer_instruction_version') or '-'}")
    if report["purge"]:
        p = report["purge"]
        lines.append(f"- 清理:clean={p['clean']},剩余点 {p['points_left']}")
    lines.append("")

    build = report["build"]
    lines.append("## 构建有效性")
    if build is None:
        lines.append("缺少 build.json:本 run 的构建状态未知。")
    else:
        lines.append(f"- 完成(complete):{build['complete']};队列收尾(drained):{build['drained']};"
                     f"驱动轮数 {build['ticks']};写入记忆 {build['memories']} 条")
        lines.append(f"- 失败任务 {build['jobs_failed']};待索引 {build['index_pending']};"
                     f"清理台账 {build['cleanup'] or '-'}")
        if build.get("last_error"):
            lines.append(f"- 最近错误:{build['last_error']}")
        if not build["complete"]:
            lines.append("- **构建未完成:本 run 的结果不得当作正常质量汇总。**")
    lines.append("")

    summary = report.get("summary")
    lines.append("## 三模式对照")
    if not summary:
        lines.append("缺少 scores.json:先运行 `score` 再重新生成报告。")
    else:
        lines.append("| 模式 | 已评分 | 失败 | 跳过 | 平均 F1(逐题) | macro F1(类别) | "
                     "平均 BLEU-1 | judge 准确率 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for mode, s in (summary.get("by_mode") or {}).items():
            counts = s.get("counts") or {}
            lines.append(f"| {mode} | {counts.get('scored', 0)} | {counts.get('failed', 0)} | "
                         f"{counts.get('skipped', 0)} | {_fmt(s.get('mean_f1'))} | "
                         f"{_fmt(s.get('macro_f1'))} | {_fmt(s.get('mean_bleu1'))} | "
                         f"{_fmt(s.get('judge_accuracy'))} |")
        lines += ["", f"口径:{summary.get('note', '')}", ""]
        for mode, s in (summary.get("by_mode") or {}).items():
            lines.append(f"### {mode} · 分类别")
            lines.append("| 类别 | 题数 | F1 | BLEU-1 | 裁判准确率 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for key, c in (s.get("categories") or {}).items():
                lines.append(f"| {key} | {c.get('count', 0)} | {_fmt(c.get('f1'))} | "
                             f"{_fmt(c.get('bleu1'))} | {_fmt(c.get('judge_accuracy'))} |")
            lines.append("")
    lines.append("")

    lines.append("## 失败样本")
    if not report["failures"]:
        lines.append("无。")
    else:
        lines.append("| 问题 | 模式 | 类型 | 信息 |")
        lines.append("| --- | --- | --- | --- |")
        for f in report["failures"][:50]:
            lines.append(f"| {f['id']} | {f['mode']} | {f['kind']} | {f['error']} |")
        if len(report["failures"]) > 50:
            lines.append(f"| … | | | 还有 {len(report['failures']) - 50} 条,见 report.json |")
    lines.append("")

    lines.append("## 证据诊断(标准 evidence)")
    ev = report["evidence_diagnostics"]
    lines.append(f"- 有标准 evidence 的题:{ev['with_evidence']} / {ev['scored_questions']};"
                 f"共 {ev['evidence_refs']} 条引用(未对上 {ev['unresolved_refs']} 条)")
    if ev["status_counts"]:
        lines.append("- 对账状态:" + ", ".join(f"{k}={v}" for k, v in sorted(ev["status_counts"].items())))
    lines.append(f"- {ev['note']}")
    lines.append("")

    if report.get("annotations"):
        lines += ["## 人工复核标注", "", "以下标注不改动原始评分或统计分母。", ""]
        for entry in report["annotations"]:
            lines.append(f"- {entry['question_id']} · {entry['kind']}: {entry['note']}")
        lines.append("")

    lines.append("## 耗时与 Token 口径")
    for mode, stats in report["latency"].items():
        answer, retrieval = stats["answer"], stats["retrieval"]
        pieces = []
        total = stats.get("total")
        if total:
            pieces.append(f"总耗时 {total['p50']}/{total['p95']} ms(p50/p95,n={total['n']})")
        if answer:
            pieces.append(f"回答 {answer['p50']}/{answer['p95']} ms(p50/p95,n={answer['n']})")
        if retrieval:
            pieces.append(f"检索 {retrieval['p50']}/{retrieval['p95']} ms(n={retrieval['n']})")
        lines.append(f"- {mode}:" + (";".join(pieces) if pieces else "无延迟记录(未生成答案)"))
    tokens = report["tokens"]
    if tokens:
        lines.append(f"- 回答调用 token:已知 {tokens.get('known', 0)} 条 / 未知 {tokens.get('unknown', 0)} 条"
                     f"(input {tokens.get('input_tokens')},output {tokens.get('output_tokens')})"
                     f";{tokens.get('note', '')}")
    lines.append("- 统计方法:nearest-rank 分位数;有效样本数在括号里;缺用量记 unknown,不记 0。")
    lines.append("")

    lines.append("## 口径与局限")
    for item in report["limits"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def write_report(run_dir: Path) -> dict:
    """汇总并落盘 report.json + report.md;返回 report 内容。"""
    report = build_report(run_dir)
    (run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return report
