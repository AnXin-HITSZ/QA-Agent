#!/usr/bin/env python
"""长期记忆的离线评测入口(§14)。

一次评测分四步,各自可单独跑,中间产物落在 `data/eval/runs/<run-id>/`:

    build   导入历史会话 → 按顺序登记提取任务 → 驱动 worker 跑到队列清空
    ask     对每条问题跑三种模式,导出回答 / 检索证据 / 调用统计
    export  汇总成一份 results.json(含可复现信息:代码版本、模型、参数、数据集哈希)
    purge   清掉本次评测落在自己作用域里的全部数据(不碰正式数据)

三种模式(§14):
- `no_memory`   不注入任何历史,只带系统提示 —— 基线;
- `memory`      走**与聊天同一条路**的召回(`service.recall_for_answer`)再回答;
- `full_context` 把历史会话原文全塞进提示(受上下文窗口限制的对照)。

几条硬规矩(写在代码里,不靠人记):
- **测试集不进正式记忆,靠作用域而不是靠人记得**:评测的数据与任务全部落在
  `scope='eval:<run-id>'` 这一份里(见 models.eval_scope):正式 worker 只认 `scope=''`、
  正式检索只查 `scope=''`,评测 worker 只认自己那一个 scope —— 「评测不污染正式、正式
  不碰评测」是 SQL 条件保证的。用户维度再加一道:评测 user_id 由 run-id 派生(uuid5),
  与任何真实账号不可能撞;`purge` 按 (作用域 × 该作用域内出现过的用户) 清,删不到别的。
- **构建阶段不读问题 / 答案**:`build` 只取数据集里的 `conversations`,`assert_no_leak`
  再对每一条被登记的消息做一次防呆(消息里出现问题原文即报错退出)。
- **默认不花钱**:不带 `--answer` 时 `ask` 只做召回与证据导出,一次模型调用都不发;
  批量付费评测由人显式开 `--answer` 并自选小样本。
- **不打印任何密钥**:导出文件里只有模型名与参数,配置里的 key 一律不进结果。

用法(在本机已配好 MYSQL_URL / QDRANT_URL / 凭证的环境里):

    # 随仓库带了一份合成冒烟样本(2 段会话 8 轮 + 4 条问题,不是基准数据)
    python scripts/memory_eval.py build --dataset scripts/memory_eval.sample.json --run-id smoke
    python scripts/memory_eval.py ask   --run-id smoke --modes no_memory,memory,full_context
    python scripts/memory_eval.py purge --run-id smoke

    # 端到端一条命令(默认不含模型调用;--answer 才会真的花生成费用)
    python scripts/memory_eval.py run --dataset scripts/memory_eval.sample.json --run-id smoke

数据集格式(LoCoMo 适配时按这个结构喂进来即可):

    {
      "name": "sample",
      "conversations": [
        {"id": "c1", "subject": "可选:这段会话属于谁", "turns": [{"user": "…", "assistant": "…"}, …]}
      ],
      "questions": [
        {"id": "q1", "conversation_id": "c1", "question": "…", "answer": "…", "category": ""}
      ]
    }

`subject` 可选。多主体数据集(一段会话 = 一个人)里必须写:不同 subject 的记忆分属不同
用户,不会互相召回 —— 否则 A 的历史会被塞进 B 的问题里,「记忆模式」就拿到了本不该有的
事实,分数也就没有意义了。缺省(都为空)= 整个 run 一个用户。

未验证事项(与技术方案「未验证的外部环境要求」一致):真实 MySQL / Qdrant / 付费模型
上的表现只能在配齐凭证的环境里跑出来,本仓库的自动化测试用的是替身(见
tests/test_memory_eval.py),**不构成对外部服务真实行为的验证**。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:                      # 允许直接 `python scripts/memory_eval.py`
    sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402
from app.memory import db, repo, service, vector  # noqa: E402
from app.memory.models import eval_scope  # noqa: E402

RUNS_DIR = BACKEND / "data" / "eval" / "runs"
MODES = ("no_memory", "memory", "full_context")
EVAL_NAMESPACE = uuid.UUID("6f2c1f8e-0000-4000-8000-0000000e7a10")   # 固定:同一 run-id 永远同一用户
# run-id 直接当目录名用,所以只收窄到「文件名安全」的一小撮字符(也别让它长到撑爆作用域列)
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,48}$")
# 上下文窗口的**近似**阈值(字符数)。这里刻意不折算 token(§13:不按字数猜 token)——
# 它只用来标出「全量历史已经超出窗口」这一事实,不用于计费。
DEFAULT_CONTEXT_CHARS = 24000


# ---------------------------------------------------------------- 身份与隔离


def eval_user_id(run_id: str, subject: str = "") -> str:
    """评测用户 id:由 run-id(与主体)派生 —— 同一 run 的 build / ask / purge 是同一个用户。

    带 subject 时再分一层:多主体数据集里 A 与 B 的记忆是两份,谁也召不回谁的。
    同一 run 的 build / ask / purge 用的都是这个派生规则,所以彼此对得上。
    """
    tail = f"memory-eval:{run_id}" + (f":{subject}" if subject else "")
    return str(uuid.uuid5(EVAL_NAMESPACE, tail))


def check_run_id(run_id: str) -> str:
    """run-id 既是作用域、也是目录名:空/带路径分隔符/带空格一律拒绝。

    （目录名要拼进 `data/eval/runs/<run-id>/`,不校验就等于允许 `../../` 这样的值把产物
    写到别处;作用域同理 —— 空 run-id 派生的 scope 会与正式数据撞上。）
    """
    rid = (run_id or "").strip()
    if not rid:
        raise SystemExit("run-id 不能为空:作用域与运行目录都由它派生")
    if not RUN_ID_PATTERN.match(rid):
        raise SystemExit(f"run-id 不合法:{rid!r}(只允许字母 / 数字 / . _ -,且以字母或数字开头)")
    return rid


class EvalEnv:
    """一次评测的运行环境:身份 + 作用域 + 目录 + 计时 + 结果骨架。

    隔离靠 **scope**(评测 Worker 只认这个 scope,见 worker.py)与派生 user_id,
    不再另起 Qdrant collection:索引点按 payload 里的 scope 过滤,与正式数据同集合共存,
    却谁也不出现在谁的候选里 —— 「正式 Worker 在同一个进程里跑」也不会把评测数据写进
    正式索引(反之亦然),因为写入路径本身就带 scope。
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = check_run_id(run_id)
        self.scope = eval_scope(self.run_id)
        self.user_id = eval_user_id(self.run_id)          # 缺省主体:整段数据集共用一个用户
        self.collection = vector.collection_name()        # 只记进产物,便于人工核对落在哪
        self.dir = RUNS_DIR / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.since = datetime.now(timezone.utc)

    def path(self, name: str) -> Path:
        return self.dir / name

    def write(self, name: str, payload: dict) -> Path:
        p = self.path(name)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return p


def require_ready() -> None:
    """评测要在真存储上跑:没配库 / 功能关闭 / 写被暂停时直接说清楚,别跑出半份结果。"""
    if not db.configured() or not get_settings().memory_enabled:
        raise SystemExit("长期记忆未启用:先在 backend/.env 配好 MYSQL_URL 并置 MEMORY_ENABLED=true")
    if not get_settings().memory_write_enabled:
        # 这一条最阴险:暂停写入时 enqueue_extraction 静默返回 None,构建会「跑完、没报错、
        # 一条记忆也没有」—— 假的成功比报错更难发现,所以直接拒绝。
        raise SystemExit("MEMORY_WRITE_ENABLED=false:评测的提取任务会被静默丢弃,拒绝继续"
                         "(先打开写入开关再跑评测)")


def users_in_scope(scope: str) -> list[str]:
    """该作用域内出现过的用户 id(items / jobs / 清理台账三处的并集)。"""
    with db.session_scope() as session:
        return repo.users_in_scope(session, scope=scope)


# ---------------------------------------------------------------- 数据集


def load_dataset(path: str) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    convs = raw.get("conversations") or []
    if not convs:
        raise SystemExit(f"数据集里没有 conversations:{path}")
    for c in convs:
        if not c.get("id") or not c.get("turns"):
            raise SystemExit("每个 conversation 都要有 id 与 turns")
        if c.get("subject") is not None and not isinstance(c["subject"], str):
            raise SystemExit(f"conversation {c['id']} 的 subject 必须是字符串")
        for t in c["turns"]:
            if not (t.get("user") and t.get("assistant")):
                raise SystemExit(f"conversation {c['id']} 里有缺少 user / assistant 的轮次")
    for q in raw.get("questions") or []:
        if not q.get("id") or not q.get("question"):
            raise SystemExit("每个 question 都要有 id 与 question")
    raw["_path"] = str(Path(path).resolve())
    raw["_digest"] = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    return raw


def exchanges(conversations: list[dict]) -> list[dict]:
    """把会话摊平成「一问一答」——登记提取任务的最小单位(与聊天入口一致)。

    带上 `subject`(缺省为空串):它决定这份记忆归谁 —— 多主体数据集里两个人各自一份,
    互相召不回来(见模块 docstring 的数据集格式说明)。
    """
    out = []
    for conv in conversations:
        for turn in conv["turns"]:
            out.append({"thread_id": f"eval:{conv['id']}", "user": turn["user"],
                        "assistant": turn["assistant"],
                        "subject": (conv.get("subject") or "").strip()})
    return out


def subjects_of(dataset: dict) -> list[str]:
    """数据集里出现过的 subject(去重、保序;缺省只有一个空主体)。"""
    seen: list[str] = []
    for conv in dataset.get("conversations") or []:
        subject = (conv.get("subject") or "").strip()
        if subject not in seen:
            seen.append(subject)
    return seen or [""]


def assert_no_leak(dataset: dict, messages: list[dict]) -> None:
    """防呆:构建阶段塞进提取任务的消息里,不许出现任何测试问题的原文。

    这条检查是**故意冗余**的(结构上 build 本来就只读 conversations)—— 评测最怕的
    就是「构建时偷看了问题」,这类错误一旦混进结果比跑不动更难发现。
    """
    blob = "\n".join(m.get("content", "") for m in messages)
    for q in dataset.get("questions") or []:
        if q["question"] in blob:
            raise SystemExit(f"构建阶段的消息里出现了问题原文:{q['id']} —— 拒绝继续(会把答案喂给记忆)")


# ---------------------------------------------------------------- build


def _status_all(env: EvalEnv, subjects: list[str]) -> dict:
    """把各主体(user_id)的状态**加总**成本次评测的一份(构建/索引是否收尾用)。"""
    total = {"items": 0, "index_pending": 0, "jobs": {}, "last_error": ""}
    for subject in subjects:
        st = service.status(user_id=eval_user_id(env.run_id, subject), scope=env.scope)
        total["items"] += int(st.get("items") or 0)
        total["index_pending"] += int(st.get("index_pending") or 0)
        for key, value in (st.get("jobs") or {}).items():
            total["jobs"][key] = int(total["jobs"].get(key) or 0) + int(value or 0)
        if st.get("last_error") and not total["last_error"]:
            total["last_error"] = str(st["last_error"])
    return total


def build(env: EvalEnv, dataset: dict, *, max_ticks: int = 200, sleep: float = 0.0) -> dict:
    """登记全部会话的提取任务,再驱动 worker 跑到「没有待执行 / 执行中任务」为止。

    任务带 `scope=env.scope`(正式 Worker 领不到),执行的是**同一个 scope** 的 worker:
    谁也碰不到谁的数据,不需要「跑评测前先停掉正式 worker」这种操作纪律。
    """
    from app.memory.worker import MemoryWorker

    require_ready()
    convs = dataset["conversations"]                     # 只取会话:问题与此步无关
    subjects = subjects_of(dataset)
    queued = 0
    for ex in exchanges(convs):
        messages = [{"role": "user", "content": ex["user"]},
                    {"role": "assistant", "content": ex["assistant"]}]
        assert_no_leak(dataset, messages)
        if service.enqueue_extraction(user_id=eval_user_id(env.run_id, ex["subject"]),
                                      messages=messages, thread_id=ex["thread_id"],
                                      source="eval_build", scope=env.scope):
            queued += 1

    worker = MemoryWorker(scope=env.scope)
    ticks, drained = 0, False
    while ticks < max_ticks:
        ticks += 1
        worker.run_once(limit=20, force=True)
        st = _status_all(env, subjects)
        jobs = st.get("jobs") or {}
        if not jobs.get("pending") and not jobs.get("running") and not st.get("index_pending"):
            drained = True
            break
        if sleep:
            time.sleep(sleep)

    st = _status_all(env, subjects)
    summary = {
        "run_id": env.run_id, "scope": env.scope, "collection": env.collection,
        "user_ids": {s or "(缺省)": eval_user_id(env.run_id, s) for s in subjects},
        "dataset": {"path": dataset.get("_path", ""), "digest": dataset.get("_digest", ""),
                    "name": dataset.get("name", "")},
        "conversations": len(convs), "subjects": len(subjects),
        "exchanges": len(exchanges(convs)), "jobs_queued": queued,
        "ticks": ticks, "drained": drained,
        "memories": st.get("items", 0), "index_pending": st.get("index_pending", 0),
        "jobs": st.get("jobs", {}), "last_error": st.get("last_error", ""),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    env.write("build.json", summary)
    return summary


# ---------------------------------------------------------------- ask


def _manifest(env: EvalEnv, dataset: dict | None, modes: list[str], context_chars: int) -> dict:
    """可复现信息:代码版本 + 模型 + 关键参数 + 作用域(绝不含 key)。"""
    s = get_settings()
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BACKEND, capture_output=True,
                                text=True, timeout=10).stdout.strip()
    except Exception:                                    # noqa: BLE001 —— 拿不到就留空,不猜
        commit = ""
    return {
        "code_commit": commit,
        "llm_model": s.llm_model, "llm_temperature": s.llm_temperature,
        "embeddings_model": s.embeddings_model, "embeddings_dim": s.embeddings_dim,
        "rerank_model": s.rerank_model, "rerank_enabled": bool(s.rerank_enabled),
        "memory_maintenance_enabled": bool(s.memory_maintenance_enabled),
        "memory_top_k": s.memory_top_k, "memory_vector_k": s.memory_vector_k,
        "memory_bm25_k": s.memory_bm25_k, "memory_rerank_k": s.memory_rerank_k,
        "memory_rrf_k": s.memory_rrf_k, "memory_context_chars": s.memory_context_chars,
        "modes": modes, "context_chars": context_chars,
        "scope": env.scope, "collection": env.collection,
        "user_ids": ([eval_user_id(env.run_id, s) for s in subjects_of(dataset)]
                     if dataset else [env.user_id]),
        "dataset": ({"path": dataset.get("_path", ""), "digest": dataset.get("_digest", "")}
                    if dataset else {}),
    }


def _history_block(convs: list[dict]) -> str:
    lines = []
    for conv in convs:
        for t in conv["turns"]:
            lines.append(f"用户:{t['user']}")
            lines.append(f"助手:{t['assistant']}")
    return "\n".join(lines)


def _answer(prompt: str, memory_block: str) -> str:
    """真正发一次聊天模型调用(只有 --answer 才会走到这里)。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.graph.nodes import SYSTEM_PROMPT
    from app.llm import get_llm

    system = SYSTEM_PROMPT + memory_block
    out = get_llm().invoke([SystemMessage(content=system), HumanMessage(content=prompt)])
    content = getattr(out, "content", out)
    return content if isinstance(content, str) else str(content)


def _recall(user_id: str, query: str, *, scope: str) -> dict:
    """回答前召回 + 证据,只发**一次**检索调用。

    与 `service.recall_for_answer` 同一套规则(同样的开关判断、同样的 CONTEXT_HEADER、
    同样的 format_evidence),区别只有一个:聊天路径只要参考块,评测还要证据明细,
    若不合并就会为同一条问题检索两次 —— 那是白花钱的重排与向量调用。
    """
    from app.memory import db
    from app.memory.search import TASK_ANSWER, format_evidence, search as memory_search

    if not db.enabled() or not get_settings().memory_search_enabled:
        return {"text": "", "hits": [], "degraded": ["记忆检索已关闭(配置如此)"],
                "error": "", "counts": {}}
    result = memory_search(user_id=user_id, query=query, task=TASK_ANSWER, scope=scope)
    if result.error:
        return {"text": "", "hits": [], "degraded": list(result.degraded), "error": result.error,
                "counts": {}}
    body = format_evidence(result)
    return {
        "text": f"{service.CONTEXT_HEADER}\n{body}" if body else "",
        "hits": [{"memory_id": h.memory_id, "text": h.text, "score": h.score, "origin": h.origin,
                  "rrf": h.rrf, "vector_rank": h.vector_rank, "bm25_rank": h.bm25_rank,
                  "revision": h.revision, "trimmed": h.trimmed, "thread_id": h.thread_id or "",
                  "updated_at": h.updated_at.isoformat() if h.updated_at else ""}
                 for h in result.hits],
        "degraded": list(result.degraded), "error": "", "counts": dict(result.counts),
    }


def ask(env: EvalEnv, dataset: dict, *, modes: list[str], answer: bool,
        context_chars: int = DEFAULT_CONTEXT_CHARS) -> dict:
    """逐条问题跑指定模式。不带 `answer=True` 时只做召回与证据导出(零模型调用)。

    `memory` 模式召回的是**这道题所属主体**的记忆(按 `conversation_id` → subject 找),
    不是「整个 run 一个大池子」:多主体数据集里那等于把别人的历史喂进这道题。
    """
    require_ready()
    by_id = {c["id"]: c for c in dataset["conversations"]}
    q_user = {c["id"]: eval_user_id(env.run_id, (c.get("subject") or "").strip())
              for c in dataset["conversations"]}
    results = []
    for q in dataset.get("questions") or []:
        history = [by_id[q["conversation_id"]]] if q.get("conversation_id") in by_id else dataset["conversations"]
        block = _history_block(history)
        ask_user = q_user.get(q.get("conversation_id"), env.user_id)
        row = {"id": q["id"], "conversation_id": q.get("conversation_id", ""),
               "question": q["question"], "reference": q.get("answer", ""),
               "category": q.get("category", ""), "memory_user": ask_user, "modes": {}}

        if "no_memory" in modes:
            row["modes"]["no_memory"] = {
                "answer": _answer(q["question"], "") if answer else "",
                "context_chars": 0,
            }

        if "memory" in modes:
            recall = _recall(ask_user, q["question"], scope=env.scope)
            row["modes"]["memory"] = {
                "answer": _answer(q["question"], recall["text"]) if answer else "",
                "recall_count": len(recall["hits"]), "degraded": recall["degraded"],
                "error": recall["error"], "counts": recall["counts"],
                "context_chars": len(recall["text"]),
                "evidence": recall["hits"],          # 检索证据:排序依据、两路名次、版本都在
            }

        if "full_context" in modes:
            row["modes"]["full_context"] = {
                "answer": _answer(q["question"], f"\n\n【历史对话】\n{block}") if answer else "",
                "history_chars": len(block), "over_window": len(block) > context_chars,
                "truncated": False,          # 不静默截断:超窗如实标注,由人决定怎么处理
                # 喂的是哪一段历史(与隔离用的 scope 无关,所以不叫 scope)
                "history_scope": "question_conversation" if q.get("conversation_id") in by_id
                                 else "all",
            }
        results.append(row)

    out = {"run_id": env.run_id, "scope": env.scope, "collection": env.collection,
           "user_ids": sorted({row["memory_user"] for row in results}) or [env.user_id],
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "answer_calls": bool(answer), "questions": results,
           "usage": usage_stats(env)}
    env.write("results.json", out)
    return out


# ---------------------------------------------------------------- 调用统计


def usage_stats(env: EvalEnv) -> dict:
    """本次窗口内记忆相关的调用与估算费用(§13)。

    口径与「调用与费用」页一致:金额是**估算**(缺用量 / 缺价格一律记 unknown,不记 0);
    过滤条件是「窗口 + 记忆用途」—— 同一窗口内其它用户的记忆调用也会被计入,
    所以评测应独占环境跑(这一点写进结果文件,也写进部署指南)。
    """
    from app.metering import writer as metering_writer
    from app.metering.context import (
        PURPOSE_MEMORY_EXTRACT, PURPOSE_MEMORY_MAINTENANCE, PURPOSE_MEMORY_SEARCH,
    )
    from app.metering.store import CallFilter, get_store

    purposes = [PURPOSE_MEMORY_EXTRACT, PURPOSE_MEMORY_MAINTENANCE, PURPOSE_MEMORY_SEARCH]
    out = {"since": env.since.isoformat(timespec="seconds"), "by_purpose": {},
           "note": "同一时间窗内其它用户的记忆调用也会计入;评测应独占环境",
           "totals": {"calls": 0, "failure": 0, "unknown_cost": 0},
           "costs": []}
    try:
        metering_writer.get_writer().flush()             # 队列里可能有还没落库的事件
        store = get_store()
        for purpose in purposes:
            s = store.summary(CallFilter(since=env.since, purpose=purpose))
            t = s.get("totals") or {}
            out["by_purpose"][purpose] = {"calls": int(t.get("calls") or 0),
                                          "failure": int(t.get("failure") or 0),
                                          "unknown_cost": int(t.get("unknown_cost") or 0),
                                          "by_service": s.get("by_service") or []}
            for k in ("calls", "failure", "unknown_cost"):
                out["totals"][k] += out["by_purpose"][purpose][k]
            out["costs"].extend(s.get("cost_by_service_currency") or [])
        # 生成回答的那次聊天调用不带 job_id,用途也不是 memory_*:如实说明它没被计进来
        out["note"] += ";--answer 产生的聊天模型调用不在这三项用途里(见结果文件的 answer_calls)"
    except Exception as exc:                             # noqa: BLE001 —— 统计失败不拖垮评测
        out["error"] = f"{type(exc).__name__}"           # 只留类名:异常文本可能带 SQL 参数
    return out


# ---------------------------------------------------------------- purge


def purge(env: EvalEnv) -> dict:
    """清掉本次评测落在 `scope` 里的全部数据(只动这一个作用域)。

    圈定范围的办法是**反查**:该作用域里出现过哪些用户(items / jobs / 台账的并集),
    逐个按 (user_id, scope) 清 —— 主体有几个不由脚本假设,数据和任务一起走。
    向量按 (user_id, scope, 代次上界) 删;不删集合(正式的索引点与评测点同集合共存,
    删集合就等于删掉生产索引)。
    """
    require_ready()
    users = users_in_scope(env.scope)
    if not users:
        users = [env.user_id]                            # 这次什么都没跑过:也如实报一个空清
    cleared, errors = [], []
    for user_id in users:
        try:
            result = service.clear_user(user_id=user_id, scope=env.scope)
        except Exception as exc:                         # noqa: BLE001 —— 清不干净要如实报,不假装
            errors.append(f"{user_id}:{type(exc).__name__}")
            continue
        result["user_id"] = user_id
        cleared.append(result)
    left = vector.count(scope=env.scope)                 # 本作用域还剩几个点(应为 0)
    out = {"run_id": env.run_id, "scope": env.scope, "collection": env.collection,
           "users": users, "cleared": cleared, "errors": errors, "points_left": left,
           "clean": (not errors) and left == 0}
    env.write("purge.json", out)
    return out


# ---------------------------------------------------------------- CLI


def cmd_build(args: argparse.Namespace) -> int:
    env = EvalEnv(args.run_id)
    dataset = load_dataset(args.dataset)
    # 原样留档(含 _path / _digest):之后单独跑 ask 不必再指一遍数据集,清单里的哈希也对得上
    env.write("dataset.json", dataset)
    summary = build(env, dataset, max_ticks=args.max_ticks, sleep=args.sleep)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["drained"] else 1


def cmd_ask(args: argparse.Namespace) -> int:
    env = EvalEnv(args.run_id)
    modes = [m for m in args.modes.split(",") if m]
    bad = [m for m in modes if m not in MODES]
    if bad:
        raise SystemExit(f"未知模式:{bad}(可选 {MODES})")
    if args.dataset:
        dataset = load_dataset(args.dataset)
    else:
        prev = json.loads(env.path("dataset.json").read_text(encoding="utf-8")) \
            if env.path("dataset.json").exists() else None
        if prev is None:
            raise SystemExit("没给 --dataset,也没有上次留下的 dataset.json:请显式指定数据集")
        dataset = prev
    out = ask(env, dataset, modes=modes, answer=args.answer, context_chars=args.context_chars)
    env.write("run.json", _manifest(env, dataset, modes, args.context_chars))
    print(f"已导出 {len(out['questions'])} 条问题 × {len(modes)} 模式 → {env.path('results.json')}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    env = EvalEnv(args.run_id)
    dataset = load_dataset(args.dataset)
    # 原样留档(含 _path / _digest):之后单独重跑 ask 时,清单里的数据集哈希与路径仍然对得上
    env.write("dataset.json", dataset)
    summary = build(env, dataset, max_ticks=args.max_ticks, sleep=args.sleep)
    modes = [m for m in args.modes.split(",") if m]
    out = ask(env, dataset, modes=modes, answer=args.answer, context_chars=args.context_chars)
    env.write("run.json", _manifest(env, dataset, modes, args.context_chars))
    purge_result = None if args.keep else purge(env)
    print(json.dumps({"build": summary, "questions": len(out["questions"]), "modes": modes,
                      "results": str(env.path("results.json")),
                      "purged": False if args.keep else bool(purge_result["clean"])},
                     ensure_ascii=False, indent=2))
    # 构建没收尾(任务还排着 / 索引还没同步)时如实返回非 0:半份数据也能接着提问,
    # 但「成功」这件事不能假装(与 cmd_build 同一口径)
    return 0 if summary["drained"] else 1


def cmd_purge(args: argparse.Namespace) -> int:
    print(json.dumps(purge(EvalEnv(args.run_id)), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="长期记忆离线评测入口(§14)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--run-id", required=True, help="本次评测的标识(派生专用 user_id 与 collection)")
        sp.add_argument("--max-ticks", type=int, default=200, help="驱动 worker 的最大轮数")
        sp.add_argument("--sleep", type=float, default=0.0, help="每轮之间的等待秒数(等外部服务时用)")

    b = sub.add_parser("build", help="导入历史会话并构建记忆")
    b.add_argument("--dataset", required=True)
    common(b)
    b.set_defaults(func=cmd_build)

    a = sub.add_parser("ask", help="执行问题并导出回答 / 证据 / 调用统计")
    a.add_argument("--run-id", required=True)
    a.add_argument("--dataset", default="", help="缺省用 run 目录里留下的 dataset.json")
    a.add_argument("--modes", default=",".join(MODES))
    a.add_argument("--answer", action="store_true", help="真的调模型生成回答(会产生付费调用)")
    a.add_argument("--context-chars", type=int, default=DEFAULT_CONTEXT_CHARS)
    a.set_defaults(func=cmd_ask)

    r = sub.add_parser("run", help="build + ask(默认最后自动 purge)")
    r.add_argument("--dataset", required=True)
    r.add_argument("--modes", default=",".join(MODES))
    r.add_argument("--answer", action="store_true", help="真的调模型生成回答(会产生付费调用)")
    r.add_argument("--context-chars", type=int, default=DEFAULT_CONTEXT_CHARS)
    r.add_argument("--keep", action="store_true", help="跑完不清理,留给人工看数据")
    common(r)
    r.set_defaults(func=cmd_run)

    g = sub.add_parser("purge", help="清掉本次评测的用户数据与索引")
    g.add_argument("--run-id", required=True)
    g.set_defaults(func=cmd_purge)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
