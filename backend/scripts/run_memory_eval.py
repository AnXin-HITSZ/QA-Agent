"""Development-only evaluation convenience runner; delegates to memory_eval.py.

No automatic purge or paid calls on import/help/dry-run. The underlying CLI owns
scope isolation, build gates, model calls, scoring and deletion recovery.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import urlsplit
import uuid

BACKEND = Path(__file__).resolve().parents[1]
RUNS = BACKEND / "data/eval/runs"
LATEST = BACKEND / "data/eval/last-run.json"
CLI = BACKEND / "scripts/memory_eval.py"
DEFAULT_DATASET = BACKEND / "data/eval/datasets/locomo/locomo-smoke.json"
DEFAULT_INPUT = BACKEND / "data/eval/datasets/locomo/locomo10.json"
ACTIONS = ("full", "build", "ask", "score", "report", "status", "purge", "compare")


def checked_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,48}", value):
        raise ValueError("run-id 需为 1–49 个字母、数字、点、下划线或连字符，以字母或数字开头")
    return value


def resolve_id(action: str, explicit: str | None) -> str:
    if explicit:
        return checked_id(explicit)
    if action in ("full", "build"):
        return "locomo-auto-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    if not LATEST.exists():
        raise ValueError("没有最近运行记录，请用 --run-id 指定实验")
    return checked_id(json.loads(LATEST.read_text(encoding="utf-8"))["run_id"])


def plan(args, run_id: str) -> list[list[str]]:
    common = ["--run-id", run_id]
    build = ["build", "--dataset", str(args.dataset), *common, "--sleep", "1"]
    ask = ["ask", *common, "--modes", "no_memory,memory,full_context",
           "--context-chars", str(args.context_chars), "--answer"]
    score = ["score", *common, "--metrics", "f1,bleu1" if args.no_judge else "f1,bleu1,judge"]
    if not args.no_judge:
        score.append("--judge-use-answer-model")
    report = ["report", *common]
    if getattr(args, "variant", ""):
        for command in (ask, score, report):
            command.extend(["--variant", args.variant])
    commands = {"full": [build, ask, score, report], "build": [build], "ask": [ask],
            "score": [score, report], "report": [report], "purge": [["purge", *common]],
            "status": [], "compare": [["compare", *common, "--variants", getattr(args, "variants", "nograph,graph")]]}[args.action]
    if getattr(args, "full_dataset", False):
        commands.insert(0, ["prepare", "--input", str(args.input), "--output", str(args.dataset),
                            "--sample-limit", "0"])
    return commands


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def settings_and_environment():
    # No shell sourcing of .env; use the same Settings loader as the application.
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["PYTHONIOENCODING"] = "utf-8"
    from app.config import get_settings
    settings = get_settings()
    host = urlsplit(settings.qdrant_url or "").hostname
    bypass = [host or "", "localhost", "127.0.0.1", os.environ.get("NO_PROXY", ""),
              os.environ.get("no_proxy", "")]
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(x for x in bypass if x)
    return settings


def preflight(settings, run_id: str) -> dict:
    from sqlalchemy import create_engine, inspect, text
    if settings.app_env != "dev" or settings.memory_collection != "user_memory_eval_dev":
        raise ValueError("本脚本仅用于 dev 环境和 user_memory_eval_dev 集合，请检查 backend/.env")
    engine = create_engine(settings.mysql_url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as conn:
            database = conn.execute(text("SELECT DATABASE()")).scalar()
            if database != "qa_agent_dev":
                raise ValueError("当前连接不是 qa_agent_dev，拒绝运行")
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            required = {"memory_items", "memory_history", "memory_jobs", "memory_user_state",
                        "memory_sources", "memory_ops"}
            if required - tables:
                raise ValueError("缺少记忆表，请先执行数据库迁移")
            for table, names in {"memory_items": {"fact_context"},
                                 "memory_history": {"old_context", "new_context"}}.items():
                if not names <= {c["name"] for c in inspector.get_columns(table)}:
                    raise ValueError("缺少 0008 字段，请先执行迁移")
            if inspector.get_pk_constraint("memory_user_state")["constrained_columns"] != ["user_id", "scope"]:
                raise ValueError("记忆状态表主键不符，请核对 0006 迁移")
    finally:
        engine.dispose()
    target = {"database": database, "collection": settings.memory_collection}
    identity = RUNS / run_id / "runner.json"
    if identity.exists():
        original = json.loads(identity.read_text(encoding="utf-8")).get("target")
        if original and original != target:
            raise ValueError("本次连接目标与运行记录不一致，拒绝执行")
    print("开发库、测试集合配置和迁移字段检查通过", flush=True)
    return target


def show_status(settings, run_id: str) -> None:
    from sqlalchemy import create_engine, text
    engine = create_engine(settings.mysql_url, connect_args={"connect_timeout": 5})
    scope = "eval:" + run_id
    try:
        with engine.connect() as conn:
            jobs = [dict(r) for r in conn.execute(text("""
                SELECT status, COUNT(*) AS count FROM memory_jobs
                WHERE scope=:scope GROUP BY status
            """), {"scope": scope}).mappings()]
            latest = [dict(r) for r in conn.execute(text("""
                SELECT thread_id,status,attempts,created_at,updated_at,finished_at,last_error
                FROM memory_jobs WHERE scope=:scope ORDER BY created_at DESC LIMIT 5
            """), {"scope": scope}).mappings()]
            count = conn.execute(text("SELECT COUNT(*) FROM memory_items WHERE scope=:scope AND status='active'"),
                                 {"scope": scope}).scalar()
        print(json.dumps({"run_id": run_id, "scope": scope, "jobs": jobs,
                          "recent_jobs": latest, "active_memories": count},
                         ensure_ascii=False, indent=2, default=str))
    finally:
        engine.dispose()
    build_file = RUNS / run_id / "build.json"
    if build_file.exists():
        build = json.loads(build_file.read_text(encoding="utf-8"))
        print("构建摘要:", json.dumps({k: build.get(k) for k in
              ("complete", "exchanges", "exchanges_processed", "index_pending", "jobs_failed")}, ensure_ascii=False))
    else:
        print("尚未生成 build.json；数据库只显示已登记的会话，不代表全部历史会话数。")


def execute(commands: list[list[str]], run_dir: Path, record: dict) -> int:
    for command in commands:
        step = command[0]
        print(f"\n>> {step} 开始（{datetime.now().isoformat(timespec='seconds')}）", flush=True)
        record.update(step=step, state="running")
        write_json(run_dir / "runner.json", record)
        started = time.monotonic()
        try:
            result = subprocess.run([sys.executable, "-u", str(CLI), *command], cwd=BACKEND)
        except KeyboardInterrupt:
            record.update(state="interrupted")
            write_json(run_dir / "runner.json", record)
            print("已中断，保留任务和文件；用 --action status 检查，再决定恢复或清理。")
            return 130
        if result.returncode:
            record.update(state="failed", exit_code=result.returncode)
            write_json(run_dir / "runner.json", record)
            print(f"{step} 返回非零退出码，停止后续步骤，保留诊断数据。", flush=True)
            return result.returncode
        print(f">> {step} 完成，耗时 {time.monotonic() - started:.1f} 秒", flush=True)
        if step == "prepare":
            path = Path(command[command.index("--output") + 1])
            dataset = json.loads(path.read_text(encoding="utf-8"))
            conversations = dataset["conversations"]
            print(f"全量适配结果：{len(conversations)} 个样本、"
                  f"{sum(len(c.get('sessions', [])) for c in conversations)} 个会话、"
                  f"{len(dataset['questions'])} 道题（包括不可回答类）。", flush=True)
    record.update(state="finished", finished_at=datetime.now().isoformat(timespec="seconds"))
    write_json(run_dir / "runner.json", record)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="开发环境一键记忆评测；默认含付费构建、回答和裁判，保留测试数据")
    parser.add_argument("--action", choices=ACTIONS, default="full")
    parser.add_argument("--run-id", help="缺省 full/build 新建；其他动作使用最近 run")
    parser.add_argument("--variant", default="", help="同一固定构建的命名消融实验")
    parser.add_argument("--variants", default="nograph,graph", help="compare 的两个实验名称")
    parser.add_argument("--dataset", type=Path,
                        help="已 prepare 的数据集；相对路径以 backend 为基准")
    parser.add_argument("--full-dataset", action="store_true",
                        help="自动 prepare 原始 LoCoMo 全部样本及全部五类问题，再进行构建和评测")
    parser.add_argument("--input", type=Path, help="全量原始数据路径，默认 data/eval/datasets/locomo/locomo10.json")
    parser.add_argument("--context-chars", type=int, default=100000)
    parser.add_argument("--no-judge", action="store_true", help="只算 F1/BLEU-1，不调用裁判")
    parser.add_argument("--dry-run", action="store_true", help="只显示计划，不连数据库、不调用模型、不写文件")
    args = parser.parse_args(argv)
    os.chdir(BACKEND)
    try:
        if args.context_chars < 1:
            raise ValueError("context-chars 必须为正数")
        if args.full_dataset and (args.action not in ("full", "build") or args.dataset is not None):
            raise ValueError("--full-dataset 仅用于 full/build，不能同时传 --dataset")
        if args.input is not None and not args.full_dataset:
            raise ValueError("--input 需与 --full-dataset 一起使用")
        run_id = resolve_id(args.action, args.run_id)
        run_dir = RUNS / run_id
        if args.full_dataset:
            args.input = args.input or DEFAULT_INPUT
            args.dataset = run_dir / "prepared-dataset.json"
        else:
            args.dataset = args.dataset or DEFAULT_DATASET
        if args.action == "full" and run_dir.exists():
            raise ValueError("full 不覆盖已有 run；用新 ID，或 --action 指定恢复步骤")
        if args.action not in ("full", "build") and not run_dir.exists():
            raise ValueError("运行目录不存在，请确认 run-id")
        commands = plan(args, run_id)
        print("run-id:", run_id, "\n产物目录:", run_dir, flush=True)
        if args.dry_run:
            for command in commands:
                print(subprocess.list2cmdline([sys.executable, str(CLI), *command]))
            return 0
        if args.action in ("report", "compare"):
            saved = run_dir / "runner.json"
            target = json.loads(saved.read_text(encoding="utf-8")).get("target", {}) if saved.exists() else {}
        else:
            settings = settings_and_environment()
            if args.action in ("full", "build"):
                if args.full_dataset:
                    if not args.input.is_file():
                        raise ValueError("原始 LoCoMo 文件不存在，请放置 locomo10.json 或用 --input 指定")
                else:
                    from memory_eval import load_dataset
                    load_dataset(str(args.dataset))  # 输入校验先于任何构建调用
            target = preflight(settings, run_id)
            if args.action == "status":
                show_status(settings, run_id)
                return 0
        if args.action in ("full", "build", "ask", "score"):
            print("注意：构建、问答及开启的裁判会调用付费服务；不会自动删除测试数据。", flush=True)
            if args.full_dataset:
                print("全量包含所有历史和五类问题，每道题最多三次回答、三次裁判；费用与耗时明显增加。", flush=True)
        record = {"run_id": run_id, "action": args.action, "target": target,
                  "full_dataset": args.full_dataset, "dataset": str(args.dataset),
                  "started_at": datetime.now().isoformat(timespec="seconds")}
        if args.action in ("full", "build"):
            write_json(LATEST, {"run_id": run_id})
        output_dir = run_dir / "variants" / args.variant if args.variant else run_dir
        rc = execute(commands, output_dir, record)
        if not rc:
            print("\n完成。报告:", output_dir / "report.md")
            print("查看进度：bash eval-memory.sh --action status --run-id", run_id)
            print("清理服务端测试数据：bash eval-memory.sh --action purge --run-id", run_id)
        return rc
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # 不输出连接 URL、密码或供应商错误正文。
        print(f"运行检查失败（{type(exc).__name__}），请检查隧道、开发配置和数据文件。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
