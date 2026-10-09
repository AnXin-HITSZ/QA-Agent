#!/usr/bin/env python
"""长期记忆的评测入口:LoCoMo 适配 / 构建 / 对照提问 / 本地评分 / 报告 / 清理。

一次评测分六步,各自可单独跑,中间产物落在 `data/eval/runs/<run-id>/`:

    prepare 原始 LoCoMo 数据 → 评测数据集(纯文件,不连库、不调模型)
    build   导入历史会话 → 按顺序登记提取任务 → 驱动 worker 跑到收尾
    ask     对每条问题跑三种模式,导出回答 / 检索证据 / 耗时与 token
    score   F1 / BLEU-1 本地评分;LLM 裁判**显式开启**才调用(prepare/score/report 不连库)
    report  汇总成 report.json + report.md(失败样本与分类比较在内)
    purge   清掉本次评测落在自己作用域里的全部数据(不碰正式数据)

三种模式(仅上下文来源不同,其余全同):
- `no_memory`   不带任何历史/记忆,只带统一的评测回答指令 —— 基线;
- `memory`      走**与聊天同一条路**的召回(检索 / RRF / 重排 / 正文预算)再回答;
- `full_context` 把该题所属会话的全部历史原文塞进提示(超预算跳过回答调用,不静默截断)。

几条硬规矩(写在代码里,不靠人记):
- **测试集不进正式记忆,靠作用域而不是靠人记得**:评测的数据与任务全部落在
  `scope='eval:<run-id>'` 这一份里(见 models.eval_scope),用户维度由 run-id 派生
  (uuid5),purge 按 (作用域 × 该作用域内的用户) 反查清理,删不到别的。
- **构建阶段不读问题 / 答案**:`build` 只取数据集里的会话;`assert_no_leak` 再做一次
  防呆(消息里出现问题原文即报错退出)。
- **构建没收尾就不进入评测**:队列收尾 / 失败任务 / 待索引 / 清理台账四项分别记录,
  任一不干净(complete=false)时 `ask` 默认拒绝;`--allow-incomplete` 只能用于诊断,
  结果会标记 run_valid=false 且退出码非 0。
- **单独跑 ask 要核对构建**:run 目录里必须有 build.json,且数据集摘要与构建时一致
  —— 换了数据集不能接着用旧记忆。
- **问题的 conversation_id 必须命中**:未知引用直接报错,不回退到全部历史 / 默认主体。
- **默认不生成答案**:不带 `--answer` 时 `ask` 只做召回与证据导出;评分默认只算本地词汇
  指标,`judge` 必须显式列进 `--metrics` 才发裁判调用。
- **不打印任何密钥**:导出文件里只有模型名与参数,配置里的 key 一律不进结果。

用法(本机已配好 MYSQL_URL / QDRANT_URL / 凭证的环境):

    # 1) LoCoMo 数据适配(把原论文仓库的 data/locomo10.json 指进来)
    python scripts/memory_eval.py prepare --input /path/to/locomo10.json \
        --output data/locomo-prepared.json [--sample-limit 1]

    # 2) 构建记忆(会调用提取模型;小样本先行)
    python scripts/memory_eval.py build --dataset data/locomo-prepared.json --run-id locomo-smoke-001

    # 3) 对照提问(--answer 才会真的花生成费用)
    python scripts/memory_eval.py ask --run-id locomo-smoke-001 \
        --modes no_memory,memory,full_context --answer

    # 4) 本地评分;要裁判再加 --metrics f1,bleu1,judge(会产生付费调用)
    python scripts/memory_eval.py score --run-id locomo-smoke-001
    python scripts/memory_eval.py report --run-id locomo-smoke-001

    # 5) 清理本次评测的数据(保留结果文件)
    python scripts/memory_eval.py purge --run-id locomo-smoke-001

    # 随仓库带了一份合成冒烟样本(2 段会话 8 轮 + 4 条问题,不是基准数据)
    python scripts/memory_eval.py run --dataset scripts/memory_eval.sample.json --run-id smoke

数据集格式(v1,本仓库自定义;`subject` 可选,多主体时必须写):

    {"name": "sample",
     "conversations": [{"id": "c1", "subject": "…", "turns": [{"user": "…", "assistant": "…"}]}],
     "questions": [{"id": "q1", "conversation_id": "c1", "question": "…", "answer": "…",
                    "category": "…"}]}

数据集格式(v2,LoCoMo 适配产物,见 scripts/evalkit/locomo.py;一题一主体):

    {"schema_version": 2,
     "conversations": [{"id": "locomo:conv-26", "subject": "locomo:conv-26",
                        "speakers": {"a": "Caroline", "b": "Melanie"},
                        "sessions": [{"index": 1, "date_time_raw": "1:56 pm on 8 May, 2023",
                                      "turns": [{"dia_id": "D1:1", "speaker": "Caroline",
                                                 "text": "…"}]}]}],
     "questions": [{"id": "locomo:conv-26:q1", "conversation_id": "locomo:conv-26",
                    "question": "…", "answer": "…", "category": 4,
                    "evidence": ["D1:1"]}]}

评分口径(全部落在 scores.json / report.md,不得含糊):
- `f1` 与官方 evaluation.py 对齐口径但**不做词干化**,分数不可与官方结果直接对比;
- `bleu1` 是本仓库补充指标(官方没有 BLEU);
- `judge` 显式开启;失败记 judge_error,不记 0 / 不正确;
- 失败 / 跳过 / 没有参考答案的题不记 0 分,剔除出分母并单独计数;
- 总体同时给出 mean(逐题平均)与 macro(类别宏平均),两个名字不混用。

未验证事项:真实 MySQL / Qdrant / 付费模型上的端到端表现只能在配齐凭证的环境里跑出来,
本仓库的自动化测试用替身(tests/test_memory_eval.py、tests/test_memory_eval_locomo.py),
**不构成对外部服务真实行为的验证**;不宣称复现 LoCoMo 官方或 Mem0 论文的任何成绩。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for _extra in (str(BACKEND), str(SCRIPTS)):           # 允许直接 `python scripts/memory_eval.py`
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

from app.config import get_settings  # noqa: E402
from app.memory import db, repo, service, vector  # noqa: E402
from app.memory.models import eval_scope  # noqa: E402
from evalkit import locomo, reporting, scoring  # noqa: E402

RUNS_DIR = BACKEND / "data" / "eval" / "runs"
MODES = ("no_memory", "memory", "full_context")
EVAL_NAMESPACE = uuid.UUID("6f2c1f8e-0000-4000-8000-0000000e7a10")   # 固定:同一 run-id 永远同一用户
# run-id 直接当目录名用,所以只收窄到「文件名安全」的一小撮字符(也别让它长到撑爆作用域列)
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,48}$")
# 上下文窗口的**近似**阈值(字符数)。这里刻意不折算 token(§13:不按字数猜 token)——
# 它只用来标出「全量历史已经超出窗口」这一事实,不用于计费。
DEFAULT_CONTEXT_CHARS = 24000

# 评测回答指令:**三种模式共用**,只有上下文来源不同(公平性要求,见模块 docstring)。
# 与聊天页的实验室助手提示词不同:不引导任何工具 / SOP 行为,只要求依据证据简短作答。
INSTRUCTION_VERSION = "eval-answer/3"
EVAL_ANSWER_INSTRUCTION = (
    "你是评测中的问答助手。请只依据下面提供的参考信息回答用户的问题(参考信息可能为空)。\n"
    "要求:\n"
    "- Always answer in English, including when reference information is in another language.\n"
    "- 回答保持简短:一个短语或一句话,不要展开解释;\n"
    "- 时间答案保留已有精度；相对时间有记录时间作参照时，可回答相对表达及其参照日期。未解析为精确日期不代表没有依据，不能补造具体日期。\n"
    "- 只依据参考信息作答;参考信息里没有依据时,直接用英文回答「Cannot be determined from the provided information」;\n"
    "- 不要编造,不要使用外部知识,不要调用任何工具或技能。"
)


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
        p.parent.mkdir(parents=True, exist_ok=True)
        temporary = p.with_name(f".{p.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(p)
        finally:
            temporary.unlink(missing_ok=True)
        return p


def require_ready(*, needs_write: bool = True) -> None:
    """评测要在真存储上跑:没配库 / 功能关闭 / 写被暂停时直接说清楚,别跑出半份结果。"""
    if not db.configured() or not get_settings().memory_enabled:
        raise SystemExit("长期记忆未启用:先在 backend/.env 配好 MYSQL_URL 并置 MEMORY_ENABLED=true")
    if needs_write and not get_settings().memory_write_enabled:
        # 这一条最阴险:暂停写入时 enqueue_extraction 静默返回 None,构建会「跑完、没报错、
        # 一条记忆也没有」—— 假的成功比报错更难发现,所以直接拒绝。
        raise SystemExit("MEMORY_WRITE_ENABLED=false:评测的提取任务会被静默丢弃,拒绝继续"
                         "(先打开写入开关再跑评测)")


def users_in_scope(scope: str) -> list[str]:
    """该作用域内出现过的用户 id(items / jobs / 清理台账三处的并集)。"""
    with db.session_scope() as session:
        return repo.users_in_scope(session, scope=scope)


# ---------------------------------------------------------------- 数据集


def dataset_digest(raw: dict) -> str:
    """数据集内容摘要(**与路径无关**):`_` 前缀的元数据(路径/摘要自身)不参与。

    这样 `build` 写下的 dataset.json 再被 `ask` 读回时,摘要与构建时一致 —— 换数据集
    冒充旧记忆会在 ask 那道闸上被拦下。
    """
    clean = {k: v for k, v in raw.items() if not str(k).startswith("_")}
    if isinstance(clean.get("source"), dict):
        clean["source"] = {k: v for k, v in clean["source"].items()
                           if k not in ("prepared_at", "input_path", "notes")}
    return hashlib.sha256(
        json.dumps(clean, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _validate_v1(raw: dict) -> None:
    convs = raw.get("conversations") or []
    if not convs:
        raise SystemExit("数据集里没有 conversations")
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


def _validate_v2(raw: dict) -> None:
    """schema v2(= LoCoMo 适配产物)的结构校验;一题一主体。"""
    convs = raw.get("conversations") or []
    if not convs:
        raise SystemExit("数据集里没有 conversations")
    known: set[str] = set()
    for c in convs:
        if not c.get("id") or not c.get("sessions"):
            raise SystemExit("schema v2 的 conversation 都要有 id 与 sessions")
        if not (c.get("subject") or "").strip():
            raise SystemExit(f"conversation {c['id']} 缺 subject(schema v2 要求一题一主体)")
        speakers = c.get("speakers") or {}
        if not speakers.get("a") or not speakers.get("b") or speakers.get("a") == speakers.get("b"):
            raise SystemExit(f"conversation {c['id']} 的 speakers.a / speakers.b 缺失或相同")
        known.add(c["id"])
        for s in c["sessions"]:
            if not isinstance(s.get("index"), int) or not s.get("turns"):
                raise SystemExit(f"conversation {c['id']} 有缺少 index / turns 的会话")
            for t in s["turns"]:
                if not (t.get("dia_id") and t.get("speaker") and t.get("text")):
                    raise SystemExit(f"conversation {c['id']} 里有缺少 dia_id / speaker / text 的消息")
    for q in raw.get("questions") or []:
        if not q.get("id") or not q.get("question"):
            raise SystemExit("每个 question 都要有 id 与 question")
        if q.get("conversation_id") not in known:
            raise SystemExit(f"question {q.get('id')} 的 conversation_id 不在 conversations 里"
                             "(拒绝回退到全部历史)")


def load_dataset(path: str) -> dict:
    """读并校验数据集(v1 / v2),附上来源路径与内容摘要(不含任何密钥)。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"数据集顶层应是对象(含 conversations / questions):{path}")
    version = raw.get("schema_version") or 1
    if version == 2:
        _validate_v2(raw)
    elif version == 1:
        _validate_v1(raw)
    else:
        raise SystemExit(f"不认识的数据集 schema_version:{version!r}(只支持 1 / 2)")
    raw["_path"] = str(Path(path).resolve())
    raw["_digest"] = dataset_digest(raw)
    return raw


def exchanges(dataset: dict | list) -> list[dict]:
    """把数据集摊平成「提取任务单元」,按历史顺序排列(构建按这个顺序逐个驱动 worker)。

    参数可以是完整数据集(dict,带 schema_version)或直接的 conversations 列表
    (v1 的旧调用方式,兼容保留)。

    - v1(本仓库自定义):一轮「一问一答」= 一个单元(与聊天入口一致);
    - v2(LoCoMo 适配):**一个会话 = 一个单元**,会话按数字序号升序(session_2 必须先于
      session_10);单元内每条消息都映射为 user 角色、正文带「姓名:」前缀(见
      evalkit.locomo.render_turn),随任务带评测适配说明(EXTRACT_NOTE)。

    每个单元带 `thread_id` / `subject` / `messages` / 可选的 `extract_note`;v2 额外带
    稳定的 turn_id 与 dedupe_key(同一会话重复 build 不会重复付费)。
    """
    conversations = dataset["conversations"] if isinstance(dataset, dict) else dataset
    units: list[dict] = []
    for conv in conversations:
        subject = (conv.get("subject") or "").strip()
        if conv.get("sessions"):
            for session in sorted(conv["sessions"], key=lambda s: int(s["index"])):
                thread = f"eval:{conv['id']}:s{session['index']}"
                units.append({
                    "thread_id": thread, "subject": subject,
                    "turn_id": thread, "dedupe_key": thread,
                    "messages": locomo.session_messages(session),
                    "extract_note": locomo.EXTRACT_NOTE,
                })
            continue
        for turn in conv["turns"]:
            units.append({
                "thread_id": f"eval:{conv['id']}", "subject": subject,
                "user": turn["user"], "assistant": turn["assistant"],
                "messages": [{"role": "user", "content": turn["user"]},
                             {"role": "assistant", "content": turn["assistant"]}],
                "extract_note": "",
            })
    return units


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
    """把各主体(user_id)的状态**加总**成本次评测的一份(构建/索引/清理是否收尾用)。"""
    total = {"items": 0, "index_pending": 0, "jobs": {}, "last_error": "",
             "cleanup_pending": 0, "cleanup_failed": 0}
    for subject in subjects:
        st = service.status(user_id=eval_user_id(env.run_id, subject), scope=env.scope)
        total["items"] += int(st.get("items") or 0)
        total["index_pending"] += int(st.get("index_pending") or 0)
        total["cleanup_pending"] += int(st.get("cleanup_pending") or 0)
        total["cleanup_failed"] += int(st.get("cleanup_failed") or 0)
        for key, value in (st.get("jobs") or {}).items():
            total["jobs"][key] = int(total["jobs"].get(key) or 0) + int(value or 0)
        if st.get("last_error") and not total["last_error"]:
            total["last_error"] = str(st["last_error"])
    return total


def build(env: EvalEnv, dataset: dict, *, max_ticks: int = 200, sleep: float = 0.0) -> dict:
    """按历史顺序登记提取任务,登记一个就把 worker 驱动到「队列收尾」再登记下一个。

    **为什么逐个驱动而不是一次全登记**(§5 顺序要求):批量登记后任务按 next_run_at
    排队,同一时刻登记的任务次序没有明确保证;按历史顺序「登记 → 等这一批跑完 → 再登记
    下一批」把顺序落在编排层,不改正式 worker 的调度语义。v2 的单元是一个会话,
    session_2 的提取与维护决策一定先于 session_10 落地。

    任务带 `scope=env.scope`(正式 Worker 领不到),执行的是**同一个 scope** 的 worker。
    收尾判据分四项记录:队列(pending/running)、失败任务、待索引、清理台账 —— 全部干净
    才是 `complete=true`;`drained` 只表示队列与索引收尾(向后兼容的字段)。
    """
    from app.memory.worker import MemoryWorker

    require_ready()
    from app.memory import extract, maintain
    identity = {"dataset_digest": dataset_digest(dataset), "collection": env.collection,
                "config": {k: v for k, v in get_settings().model_dump(mode="json").items()
                           if (k.startswith("memory_") or k.startswith("embeddings_") or k.startswith("llm_"))
                           and not any(secret in k for secret in ("key", "url", "password"))},
                "extract_note_version": locomo.EXTRACT_NOTE_VERSION,
                "extract_protocol": extract.PROTOCOL_VERSION, "maintenance_protocol": maintain.PROTOCOL_VERSION}
    identity_path = env.path("build_identity.json")
    if env.path("purge.json").exists():
        raise SystemExit("此 run 已执行清理，请使用新的 run-id 构建")
    if env.path("build.json").exists() and not identity_path.exists():
        raise SystemExit("旧 run 缺少构建身份记录，请使用新的 run-id，避免复用无法核实的任务")
    if identity_path.exists() and json.loads(identity_path.read_text(encoding="utf-8")) != identity:
        raise SystemExit("构建数据或配置发生变化，请使用新的 run-id，不能复用旧任务")
    env.write("build_identity.json", identity)
    env.write("dataset.json", dataset)
    convs = dataset["conversations"]                     # 只取会话:问题与此步无关
    subjects = subjects_of(dataset)
    units = exchanges(dataset)
    worker = MemoryWorker(scope=env.scope)
    worker.diagnostic_sink = lambda record: env.write(
        f"diagnostics/{record['job_id']}.json", record)
    ticks, queued, processed = 0, 0, 0
    drained = not units
    for unit in units:
        assert_no_leak(dataset, unit["messages"])
        if service.enqueue_extraction(user_id=eval_user_id(env.run_id, unit["subject"]),
                                      messages=unit["messages"], thread_id=unit["thread_id"],
                                      source="eval_build", scope=env.scope,
                                      turn_id=unit.get("turn_id"),
                                      dedupe_key=unit.get("dedupe_key", ""),
                                      extract_note=unit.get("extract_note", "")):
            queued += 1
        drained = False
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
        if not drained:
            break                                        # 没跑完:停下来,如实报,不再往后登记
        processed += 1

    st = _status_all(env, subjects)
    jobs = st.get("jobs") or {}
    try:                                                 # 测试替身可能没有 status();拿不到就留空
        worker_last_error = str((worker.status() or {}).get("last_error") or "")
    except Exception:                                    # noqa: BLE001
        worker_last_error = ""
    note_used = any((u.get("extract_note") or "") for u in units)
    complete = bool(drained and int(jobs.get("failed") or 0) == 0
                    and int(st.get("index_pending") or 0) == 0
                    and int(st.get("cleanup_pending") or 0) == 0
                    and int(st.get("cleanup_failed") or 0) == 0)
    summary = {
        "run_id": env.run_id, "scope": env.scope, "collection": env.collection,
        "user_ids": {s or "(缺省)": eval_user_id(env.run_id, s) for s in subjects},
        "dataset": {"path": dataset.get("_path", ""), "digest": dataset.get("_digest", ""),
                    "name": dataset.get("name", ""),
                    "schema_version": int(dataset.get("schema_version") or 1)},
        "conversations": len(convs), "subjects": len(subjects),
        "exchanges": len(units), "jobs_queued": queued, "exchanges_processed": processed,
        "ticks": ticks, "drained": drained, "complete": complete,
        "memories": st.get("items", 0), "index_pending": st.get("index_pending", 0),
        "jobs": st.get("jobs", {}), "jobs_failed": int(jobs.get("failed") or 0),
        "cleanup": {"pending": st.get("cleanup_pending", 0),
                    "failed": st.get("cleanup_failed", 0)},
        "last_error": st.get("last_error", ""), "worker_last_error": worker_last_error,
        "extract_note": {"used": note_used,
                         "version": locomo.EXTRACT_NOTE_VERSION if note_used else ""},
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    from app.memory import db, repo
    from dataclasses import asdict
    snapshot = []
    history_snapshot = []
    with db.session_scope() as session:
        for subject in subjects:
            offset = 0
            while True:
                items = repo.list_items(session, eval_user_id(env.run_id, subject),
                                        scope=env.scope, status=None, limit=200, offset=offset)
                snapshot.extend(asdict(item) for item in items)
                if len(items) < 200:
                    break
                offset += 200
            offset = 0
            while True:
                history = repo.list_history(session, eval_user_id(env.run_id, subject),
                                            scope=env.scope, limit=200, offset=offset)
                history_snapshot.extend(asdict(row) for row in history)
                if len(history) < 200:
                    break
                offset += 200
    env.write("history_snapshot.json", json.loads(json.dumps(history_snapshot, default=lambda value: value.isoformat())))
    env.write("memory_snapshot.json", json.loads(json.dumps(snapshot, default=lambda value: value.isoformat())))
    env.write("build.json", summary)
    return summary


def _build_state(env: EvalEnv, dataset: dict) -> dict:
    """ask 的构建闸:必须有 build.json,数据集摘要必须一致,构建必须干净(除非显式放行)。"""
    path = env.path("build.json")
    if env.path("purge.json").exists():
        raise SystemExit("本 run 已执行清理，不能继续检索；请使用新的 run-id")
    if not path.exists():
        raise SystemExit("没有 build.json:请先跑 build(评测必须建立在一次构建上)")
    build_summary = json.loads(path.read_text(encoding="utf-8"))
    digest = dataset.get("_digest", "")
    built_digest = (build_summary.get("dataset") or {}).get("digest", "")
    if not built_digest or digest != built_digest:
        raise SystemExit("数据集与构建时不一致(换了数据集不能复用旧记忆):先重新 build")
    jobs = build_summary.get("jobs") or {}
    current = _status_all(env, subjects_of(dataset))
    live_jobs = current.get("jobs") or {}
    live_clean = not any(int(live_jobs.get(k) or 0) for k in ("pending", "running", "failed"))
    live_clean = live_clean and not any(int(current.get(k) or 0) for k in
                                       ("index_pending", "cleanup_pending", "cleanup_failed"))
    return {"complete": bool(build_summary.get("complete")) and live_clean,
            "digest": digest,
            "jobs_failed": int(build_summary.get("jobs_failed") or jobs.get("failed") or 0),
            "index_pending": int(build_summary.get("index_pending") or 0)}


# ---------------------------------------------------------------- ask


def _worktree_state() -> tuple[bool, int]:
    """工作区是否有未提交改动(有则 manifest 记下来 —— 只记布尔与数量,不抄文件名)。"""
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=BACKEND,
                             capture_output=True, text=True, timeout=10).stdout
        lines = [line for line in out.splitlines() if line.strip()]
        return bool(lines), len(lines)
    except Exception:                                    # noqa: BLE001 —— 拿不到就取缺省,不猜
        return False, 0


def _manifest(env: EvalEnv, dataset: dict | None, modes: list[str], context_chars: int) -> dict:
    """可复现信息:代码版本 + 模型 + 关键参数 + 指令口径 + 作用域(绝不含 key)。"""
    s = get_settings()
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BACKEND, capture_output=True,
                                text=True, timeout=10).stdout.strip()
    except Exception:                                    # noqa: BLE001 —— 拿不到就留空,不猜
        commit = ""
    dirty, changed = _worktree_state()
    build_summary = None
    if env.path("build.json").exists():
        try:
            build_summary = json.loads(env.path("build.json").read_text(encoding="utf-8"))
        except (ValueError, OSError):
            build_summary = None
    return {
        "code_commit": commit, "worktree_dirty": dirty, "worktree_changed_files": changed,
        "llm_model": s.llm_model, "llm_temperature": s.llm_temperature,
        "embeddings_model": s.embeddings_model, "embeddings_dim": s.embeddings_dim,
        "rerank_model": s.rerank_model, "rerank_enabled": bool(s.rerank_enabled),
        "memory_maintenance_enabled": bool(s.memory_maintenance_enabled),
        "memory_top_k": s.memory_top_k, "memory_vector_k": s.memory_vector_k,
        "memory_bm25_k": s.memory_bm25_k, "memory_rerank_k": s.memory_rerank_k,
        "memory_rrf_k": s.memory_rrf_k, "memory_context_chars": s.memory_context_chars,
        "modes": modes, "context_chars": context_chars,
        "answer_instruction_version": INSTRUCTION_VERSION,
        "answer_instruction": EVAL_ANSWER_INSTRUCTION,
        "started_at": env.since.isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": env.scope, "collection": env.collection,
        "user_ids": ([eval_user_id(env.run_id, s) for s in subjects_of(dataset)]
                     if dataset else [env.user_id]),
        "dataset": ({"path": dataset.get("_path", ""), "digest": dataset.get("_digest", ""),
                     "schema_version": int(dataset.get("schema_version") or 1),
                     "source_kind": ((dataset.get("source") or {}).get("kind")
                                     if isinstance(dataset.get("source"), dict) else "")}
                    if dataset else {}),
        "build": ({"complete": bool(build_summary.get("complete")),
                   "drained": build_summary.get("drained"),
                   "jobs_failed": build_summary.get("jobs_failed", 0),
                   "index_pending": build_summary.get("index_pending", 0),
                   "extract_note": build_summary.get("extract_note")}
                  if build_summary else None),
    }


def _history_text(conversation: dict) -> str:
    """一段历史的全文渲染:v1(一问一答)与 v2(会话)共用一个入口,与构建输入同源。"""
    if conversation.get("sessions"):
        return "\n".join(locomo.history_lines(conversation))
    lines: list[str] = []
    for t in conversation["turns"]:
        lines.append(f"用户:{t['user']}")
        lines.append(f"助手:{t['assistant']}")
    return "\n".join(lines)


def _answer(question: str, context_block: str) -> tuple[str, dict | None, float]:
    """真正发一次聊天模型调用(只有 --answer 才会走到这里)。

    返回(正文, token 用量或 None, 毫秒耗时)。系统提示 = 统一评测指令 + 上下文块,
    三种模式的差别**只在上下文块**;用量取不到时返回 None(不记 0)。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.llm import get_llm

    system = EVAL_ANSWER_INSTRUCTION + (f"\n\n{context_block}" if context_block else "")
    started = time.monotonic()
    out = get_llm().invoke([SystemMessage(content=system), HumanMessage(content=question)])
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    content = getattr(out, "content", out)
    text = content if isinstance(content, str) else str(content)
    return text, scoring.usage_of(out), elapsed_ms


def _recall(user_id: str, query: str, *, scope: str) -> dict:
    """回答前召回 + 证据,只发**一次**检索调用。

    与 `service.recall_for_answer` 同一套规则(同样的开关判断、同样的 CONTEXT_HEADER、
    同样的 format_evidence),区别只有一个:聊天路径只要参考块,评测还要证据明细,
    若不合并就会为同一条问题检索两次 —— 那是白花钱的重排与向量调用。
    """
    from app.memory.search import TASK_ANSWER, format_evidence, search as memory_search

    if not db.enabled() or not get_settings().memory_search_enabled:
        return {"text": "", "hits": [], "degraded": ["记忆检索已关闭(配置如此)"],
                "error": "", "counts": {}}
    result = memory_search(user_id=user_id, query=query, task=TASK_ANSWER, scope=scope,
                           capture_trace=True)
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
        "trace": dict(result.trace),
    }


def _mode_no_memory(question: dict, answer: bool) -> dict:
    entry = {"answer": "", "answer_error": "", "answer_latency_ms": None,
             "answer_usage": None, "context_chars": 0, "error": ""}
    if answer:
        text, usage, ms = _answer(question["question"], "")
        entry.update(answer=text, answer_usage=usage, answer_latency_ms=ms, total_latency_ms=ms)
    return entry


def _mode_memory(env: EvalEnv, ask_user: str, question: dict, answer: bool) -> dict:
    started = time.monotonic()
    recall = _recall(ask_user, question["question"], scope=env.scope)
    retrieval_ms = round((time.monotonic() - started) * 1000, 1)
    entry = {"answer": "", "answer_error": "", "answer_latency_ms": None, "answer_usage": None,
             "retrieval_latency_ms": retrieval_ms, "total_latency_ms": retrieval_ms,
             "recall_count": len(recall["hits"]), "degraded": recall["degraded"],
             "error": recall["error"], "counts": recall["counts"],
             "context_chars": len(recall["text"]),
             "context": recall["text"],
             "evidence": recall["hits"], "retrieval_trace": recall.get("trace", {})}
    if answer:
        text, usage, ms = _answer(question["question"], recall["text"])
        entry.update(answer=text, answer_usage=usage, answer_latency_ms=ms,
                     total_latency_ms=round(retrieval_ms + ms, 1))
    return entry


def _mode_full_context(conversation: dict, question: dict, answer: bool,
                       context_chars: int) -> dict:
    block = _history_text(conversation)
    entry = {"answer": "", "answer_error": "", "answer_latency_ms": None, "answer_usage": None,
             "history_chars": len(block), "over_window": len(block) > context_chars,
             "truncated": False,          # 不静默截断:超窗如实标注,由人决定怎么处理
             "history_scope": "question_conversation", "error": ""}
    if entry["over_window"]:
        entry.update(skipped=True, skip_reason="full_context 超过字符预算，不调用回答模型")
    elif answer:
        text, usage, ms = _answer(question["question"], f"\n\n【历史对话】\n{block}")
        entry.update(answer=text, answer_usage=usage, answer_latency_ms=ms, total_latency_ms=ms)
    return entry


def _guarded(fn, *args) -> dict:
    """单题单模式失败不拖垮整轮:如实记错误,继续跑其余(失败不当空答案计分)。"""
    try:
        return fn(*args)
    except Exception as exc:                             # noqa: BLE001
        return {"answer": "", "answer_error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "answer_latency_ms": None, "answer_usage": None, "error": ""}


def _llm_usage(questions: list[dict], *, answer_calls: bool) -> dict:
    """本次 run 回答调用的 token 用量(只来自模型响应;**缺用量记 unknown,不记 0**)。

    与 `usage_stats`(计量库时间窗口径)是两回事:这里逐调用取响应里的 usage,
    裁判调用不在此列(见 scores.json)。检索 / 提取 / 重排不在此列(它们在计量库里)。
    """
    note = ("缺失一律按 unknown 计数,不记 0;不含检索 / 提取 / 重排调用(见 usage.by_purpose),"
            "也不含裁判调用(见 scores.json)")
    out = {"attempted": 0, "known": 0, "unknown": 0,
           "input_tokens": None, "output_tokens": None, "total_tokens": None,
           "by_mode": {}, "source": "模型响应的 usage_metadata / response_metadata.token_usage",
           "note": note}
    if not answer_calls:
        out["note"] = "本次未生成答案(--answer 未开):没有回答模型调用。" + note
        return out
    sums = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    have = {key: False for key in sums}
    for row in questions:
        for mode, entry in (row.get("modes") or {}).items():
            if not isinstance(entry, dict) or entry.get("skipped"):
                continue
            out["attempted"] += 1
            stat = out["by_mode"].setdefault(mode, {"attempted": 0, "known": 0, "unknown": 0})
            stat["attempted"] += 1
            usage = entry.get("answer_usage")
            if isinstance(usage, dict) and any(usage.get(k) is not None for k in sums):
                out["known"] += 1
                stat["known"] += 1
                for key in sums:
                    if usage.get(key) is not None:
                        sums[key] += usage[key]
                        have[key] = True
            else:
                out["unknown"] += 1
                stat["unknown"] += 1
    for key in sums:
        out[key] = sums[key] if have[key] else None
    return out


def _results_payload(env: EvalEnv, state: dict, modes: list[str], answer: bool,
                     results: list[dict]) -> dict:
    return {
        "run_id": env.run_id, "scope": env.scope, "collection": env.collection,
        "user_ids": sorted({row["memory_user"] for row in results}) or [env.user_id],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "answer_calls": bool(answer),
        "run_valid": bool(state["complete"]),
        "invalid_reason": ("" if state["complete"] else
                           "构建未完成 / 有失败任务 / 索引未同步(诊断性放行;结果不计入正常质量对比)"),
        "build": {"complete": state["complete"], "dataset_digest": state["digest"],
                  "jobs_failed": state["jobs_failed"], "index_pending": state["index_pending"]},
        "modes": list(modes),
        "questions": results,
        "llm_usage": _llm_usage(results, answer_calls=bool(answer)),
        "usage": usage_stats(env),
    }


def ask(env: EvalEnv, dataset: dict, *, modes: list[str], answer: bool,
        context_chars: int = DEFAULT_CONTEXT_CHARS, allow_incomplete: bool = False) -> dict:
    """逐条问题跑指定模式。不带 `answer=True` 时只做召回与证据导出(零生成调用)。

    两道闸在跑之前:
    - 每条问题的 `conversation_id` 必须命中数据集里的会话(未知引用报错,不回退全部历史);
    - 必须先有一次**干净的构建**(build.json 存在、数据集摘要一致、complete=true);
      诊断性放行要用 `allow_incomplete=True`(CLI:`--allow-incomplete`),结果标记无效。

    逐题逐模式独立 try/except:单题异常不丢已完成结果(每答一题就落一次盘),
    失败如实记录,不当空答案正常计分。
    """
    require_ready()
    by_id = {c["id"]: c for c in dataset["conversations"]}
    if not modes or any(mode not in MODES for mode in modes):
        raise SystemExit("必须指定有效评测模式")
    if context_chars < 1:
        raise SystemExit("context-chars 必须大于 0")
    unknown = [q.get("id") for q in dataset.get("questions") or []
               if not q.get("conversation_id") or q["conversation_id"] not in by_id]
    if unknown:
        raise SystemExit(f"问题引用了未知的 conversation_id,拒绝回退到全部历史:{unknown[:5]}"
                         f"(共 {len(unknown)} 条)")
    state = _build_state(env, dataset)
    if not state["complete"] and not allow_incomplete:
        raise SystemExit(
            "构建未完成 / 有失败任务 / 索引未同步:默认不进入评测"
            "(先修好后重跑 build;诊断性放行要显式加 --allow-incomplete,结果会标记为无效)")

    for filename in ("scores.json", "report.json", "report.md"):
        env.path(filename).unlink(missing_ok=True)
    results: list[dict] = []
    for q in dataset.get("questions") or []:
        conversation = by_id[q["conversation_id"]]
        ask_user = eval_user_id(env.run_id, (conversation.get("subject") or "").strip())
        row = {"id": q["id"], "conversation_id": q["conversation_id"],
               "question": q["question"], "reference": q.get("answer", ""),
               "reference_adversarial": q.get("adversarial_answer", ""),
               "category": q.get("category", ""), "category_name": q.get("category_name", ""),
               "memory_user": ask_user,
               "evidence_expected": list(q.get("evidence") or []),
               "evidence_unresolved": list(q.get("evidence_unresolved") or []),
               "evidence_status": q.get("evidence_status", ""),
               "modes": {}}
        if "no_memory" in modes:
            row["modes"]["no_memory"] = _guarded(_mode_no_memory, q, answer)
        if "memory" in modes:
            row["modes"]["memory"] = _guarded(_mode_memory, env, ask_user, q, answer)
        if "full_context" in modes:
            row["modes"]["full_context"] = _guarded(_mode_full_context, conversation, q,
                                                    answer, context_chars)
        results.append(row)
        env.write("results.json", _results_payload(env, state, modes, answer, results))
    out = _results_payload(env, state, modes, answer, results)
    env.write("results.json", out)
    return out


# ---------------------------------------------------------------- 调用统计


def usage_stats(env: EvalEnv) -> dict:
    """本次窗口内记忆相关的调用与估算费用。

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
        out["note"] += (";回答与裁判调用不经过计量库(其 token 与耗时见 results.json 的"
                        " llm_usage 与 scores.json)")
    except Exception as exc:                             # noqa: BLE001 —— 统计失败不拖垮评测
        out["error"] = f"{type(exc).__name__}"           # 只留类名:异常文本可能带 SQL 参数
    return out


# ---------------------------------------------------------------- purge


def purge(env: EvalEnv) -> dict:
    """清掉本次评测落在 `scope` 里的全部数据(只动这一个作用域)。

    圈定范围的办法是**反查**:该作用域里出现过哪些用户(items / jobs / 台账的并集),
    逐个按 (user_id, scope) 清 —— 主体有几个不由脚本假设,数据和任务一起走。
    向量按 (user_id, scope, 代次上界) 删;不删集合(正式的索引点与评测点同集合共存,
    删集合就等于删掉生产索引)。清完再让本作用域的 worker 重放几轮清理台账,
    把「事实已删、向量待清」的尾巴跑干净;仍没清掉的如实报 pending。
    """
    from app.memory.worker import MemoryWorker

    require_ready(needs_write=False)
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
    worker = MemoryWorker(scope=env.scope)
    cleanup = {"pending": 0, "failed": 0}
    for _ in range(5):                                   # 台账收尾:有活才驱动,最多 5 轮
        cleanup = {"pending": 0, "failed": 0}
        for user_id in users:
            st = service.status(user_id=user_id, scope=env.scope)
            cleanup["pending"] += int(st.get("cleanup_pending") or 0)
            cleanup["failed"] += int(st.get("cleanup_failed") or 0)
        if cleanup["pending"] == 0:
            break
        worker.run_once(limit=50, force=True)
    left = vector.count(scope=env.scope)                 # 本作用域还剩几个点(应为 0)
    cleanup = {"pending": 0, "failed": 0}
    for user_id in users:
        st = service.status(user_id=user_id, scope=env.scope)
        cleanup["pending"] += int(st.get("cleanup_pending") or 0)
        cleanup["failed"] += int(st.get("cleanup_failed") or 0)
    clean = (not errors) and left == 0 and not any(cleanup.values())
    out = {"run_id": env.run_id, "scope": env.scope, "collection": env.collection,
           "users": users, "cleared": cleared, "errors": errors, "points_left": left,
           "cleanup": cleanup,
           "status": ("clean" if clean else
                      ("pending_cleanup" if not errors and not cleanup["failed"] and cleanup["pending"] else "failed")),
           "clean": clean}
    env.write("purge.json", out)
    return out


# ---------------------------------------------------------------- CLI


def cmd_prepare(args: argparse.Namespace) -> int:
    """原始 LoCoMo 数据 → 评测数据集(纯文件操作:不连库、不调模型、不花一分钱)。"""
    try:
        dataset = locomo.prepare_file(args.input, args.output,
                                      sample_limit=args.sample_limit or None)
        limit = getattr(args, "question_limit", None)
        if limit is not None:
            if limit < 1:
                raise SystemExit("question-limit 必须大于 0")
            dataset["questions"] = dataset["questions"][:limit]
            dataset["source"]["question_limit"] = limit
            dataset["source"]["notes"].append(
                f"上述为筛选前体检；question-limit 筛选后保留 {len(dataset['questions'])} 条问题，历史不变")
            Path(args.output).write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    except locomo.LocomoDataError as exc:
        raise SystemExit(f"原始数据不满足适配要求:{exc}") from None
    print(json.dumps({
        "output": str(Path(args.output).resolve()),
        "samples": len(dataset["conversations"]),
        "questions": len(dataset["questions"]),
        "source_notes": dataset["source"]["notes"],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    env = EvalEnv(args.run_id)
    dataset = load_dataset(args.dataset)
    # 原样留档(含 _path / _digest):之后单独跑 ask 不必再指一遍数据集,清单里的哈希也对得上
    summary = build(env, dataset, max_ticks=args.max_ticks, sleep=args.sleep)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["complete"]:
        print("构建未完成(队列 / 失败任务 / 待索引 / 清理台账见上):ask 默认会拒绝进入评测;"
              "诊断性放行加 --allow-incomplete,结果会标记为无效。")
    return 0 if summary["complete"] else 1


def cmd_ask(args: argparse.Namespace) -> int:
    env = EvalEnv(args.run_id)
    modes = [m for m in args.modes.split(",") if m]
    bad = [m for m in modes if m not in MODES]
    if bad:
        raise SystemExit(f"未知模式:{bad}(可选 {MODES})")
    if args.dataset:
        dataset = load_dataset(args.dataset)
    else:
        prev = env.path("dataset.json")
        if not prev.exists():
            raise SystemExit("没给 --dataset,也没有上次留下的 dataset.json:请显式指定数据集")
        dataset = load_dataset(str(prev))
    out = ask(env, dataset, modes=modes, answer=args.answer, context_chars=args.context_chars,
              allow_incomplete=bool(getattr(args, "allow_incomplete", False)))
    env.write("run.json", _manifest(env, dataset, modes, args.context_chars))
    print(f"已导出 {len(out['questions'])} 条问题 × {len(modes)} 模式 → {env.path('results.json')}"
          + ("" if out["run_valid"] else ";**构建未通过有效检查,结果不计入正常质量对比**"))
    return 0 if out["run_valid"] else 1


def cmd_score(args: argparse.Namespace) -> int:
    """本地评分(F1 / BLEU-1);judge 只在显式列进 --metrics 时才发裁判调用。"""
    env = EvalEnv(args.run_id)
    results_path = env.path("results.json")
    if not results_path.exists():
        raise SystemExit("没有 results.json:先跑 ask(或 run)生成回答")
    results = json.loads(results_path.read_text(encoding="utf-8"))
    if results.get("run_valid") is False:
        raise SystemExit("无效构建仅供诊断，不能进行正常评分")
    metrics = [m.strip() for m in (args.metrics or "").split(",") if m.strip()]
    metrics = metrics or ["f1", "bleu1"]
    bad = [m for m in metrics if m not in ("f1", "bleu1", "judge")]
    if bad:
        raise SystemExit(f"未知指标:{bad}(可选 f1,bleu1,judge)")
    judge_llm, judge_model, judge_temperature = None, "", None
    if "judge" in metrics:
        from app.llm import get_llm

        if getattr(args, "judge_use_answer_model", False):
            judge_llm = get_llm()
            judge_model = get_settings().llm_model
            judge_temperature = get_settings().llm_temperature
        else:
            judge_model = getattr(args, "judge_model", None)
            judge_base_url = getattr(args, "judge_base_url", None)
            key = os.environ.get(getattr(args, "judge_api_key_env", "JUDGE_API_KEY"))
            if not judge_model or not judge_base_url or not key:
                raise SystemExit("裁判须独立指定 model/base-url/API key 环境变量，或显式 --judge-use-answer-model")
            from langchain_openai import ChatOpenAI
            judge_temperature = args.judge_temperature
            judge_llm = ChatOpenAI(model=judge_model, base_url=judge_base_url, api_key=key,
                                  temperature=judge_temperature,
                                  timeout=get_settings().llm_request_timeout)
        print("注意:judge 已开启 —— 每条已生成答案会多发一次裁判模型调用(付费)。")
    scores = scoring.score_results(results, metrics=metrics, judge_llm=judge_llm,
                                   judge_retries=max(0, int(args.judge_retries)),
                                   judge_model=judge_model, judge_temperature=judge_temperature)
    env.write("scores.json", scores)
    summary = scores["summary"]["by_mode"]
    print(json.dumps({mode: {"mean_f1": s["mean_f1"], "macro_f1": s["macro_f1"],
                             "mean_bleu1": s["mean_bleu1"],
                             "judge_accuracy": s["judge_accuracy"],
                             "counts": s["counts"]} for mode, s in summary.items()},
                     ensure_ascii=False, indent=2))
    print(f"已写入 {env.path('scores.json')}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """汇总 report.json + report.md(纯本地:不连库、不调模型)。"""
    env = EvalEnv(args.run_id)
    try:
        report = reporting.write_report(env.dir)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"已写入 {env.path('report.json')} 与 {env.path('report.md')}"
          f"(失败样本 {len(report['failures'])} 条;run_valid={report['run_valid']})")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    env = EvalEnv(args.run_id)
    dataset = load_dataset(args.dataset)
    modes = [m for m in args.modes.split(",") if m]
    if not modes or any(mode not in MODES for mode in modes) or args.context_chars < 1:
        raise SystemExit("模式或字符预算无效，拒绝构建")
    # 原样留档(含 _path / _digest):之后单独重跑 ask 时,清单里的数据集哈希与路径仍然对得上
    summary = build(env, dataset, max_ticks=args.max_ticks, sleep=args.sleep)
    modes = [m for m in args.modes.split(",") if m]
    if not summary["complete"]:
        # 构建没通过:不进入提问(「run 必须先 build 成功才 ask」是硬规矩)
        env.write("run.json", _manifest(env, dataset, modes, args.context_chars))
        purge_result = None if args.keep else purge(env)
        print(json.dumps({"build": {k: summary[k] for k in
                                    ("drained", "complete", "jobs_failed", "index_pending",
                                     "cleanup", "ticks", "memories")},
                          "ask": "skipped(构建未完成,拒绝提问;修好后重跑,或单跑 ask --allow-incomplete 做诊断)",
                          "purged": False if args.keep else bool(purge_result["clean"])},
                         ensure_ascii=False, indent=2))
        return 1
    out = ask(env, dataset, modes=modes, answer=args.answer, context_chars=args.context_chars)
    env.write("run.json", _manifest(env, dataset, modes, args.context_chars))
    purge_result = None if args.keep else purge(env)
    print(json.dumps({"build": {k: summary[k] for k in
                                ("drained", "complete", "jobs_failed", "index_pending",
                                 "cleanup", "ticks", "memories")},
                      "questions": len(out["questions"]), "modes": modes,
                      "results": str(env.path("results.json")),
                      "purged": False if args.keep else bool(purge_result["clean"])},
                     ensure_ascii=False, indent=2))
    # 退出码如实反映成败:构建未通过(仅可能来自诊断路径)/ 清理没干净都返回非 0
    rc = 0
    if not out["run_valid"]:
        rc = 1
    if purge_result is not None and not purge_result["clean"]:
        rc = 1
    return rc


def cmd_purge(args: argparse.Namespace) -> int:
    out = purge(EvalEnv(args.run_id))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["clean"] else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="长期记忆评测入口(prepare/build/ask/score/report/purge)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--run-id", required=True, help="本次评测的标识(派生专用 user_id 与 collection)")
        # LoCoMo 全量约有 270+ 段会话,逐个驱动 → 轮数上限给足;单段会话的样本用不到这么多
        sp.add_argument("--max-ticks", type=int, default=2000, help="驱动 worker 的最大轮数")
        sp.add_argument("--sleep", type=float, default=0.0, help="每轮之间的等待秒数(等外部服务时用)")

    pp = sub.add_parser("prepare", help="原始 LoCoMo 数据 → 评测数据集(不连库、不调模型)")
    pp.add_argument("--input", required=True, help="原始 locomo10.json 的路径")
    pp.add_argument("--output", required=True, help="评测数据集的输出路径")
    pp.add_argument("--sample-limit", type=int, default=0, help="只取前 N 个样本(0=全量)")
    pp.add_argument("--question-limit", type=int, help="保留完整历史，仅取前 N 道问题进行小样本试跑")
    pp.set_defaults(func=cmd_prepare)

    b = sub.add_parser("build", help="导入历史会话并构建记忆")
    b.add_argument("--dataset", required=True)
    common(b)
    b.set_defaults(func=cmd_build)

    a = sub.add_parser("ask", help="执行问题并导出回答 / 证据 / 耗时与 token")
    a.add_argument("--run-id", required=True)
    a.add_argument("--dataset", default="", help="缺省用 run 目录里留下的 dataset.json")
    a.add_argument("--modes", default=",".join(MODES))
    a.add_argument("--answer", action="store_true", help="真的调模型生成回答(会产生付费调用)")
    a.add_argument("--context-chars", type=int, default=DEFAULT_CONTEXT_CHARS)
    a.add_argument("--allow-incomplete", action="store_true",
                   help="构建未通过有效检查也放行(诊断用;结果标记无效,退出码非 0)")
    a.set_defaults(func=cmd_ask)

    sc = sub.add_parser("score", help="本地评分(F1 / BLEU-1);judge 需显式开启")
    sc.add_argument("--run-id", required=True)
    sc.add_argument("--metrics", default="f1,bleu1", help="逗号分隔:f1,bleu1,judge(judge 付费)")
    sc.add_argument("--judge-retries", type=int, default=1, help="裁判输出格式错误的有限重试次数")
    sc.add_argument("--judge-use-answer-model", action="store_true")
    sc.add_argument("--judge-model")
    sc.add_argument("--judge-base-url")
    sc.add_argument("--judge-api-key-env", default="JUDGE_API_KEY")
    sc.add_argument("--judge-temperature", type=float, default=0.0)
    sc.set_defaults(func=cmd_score)

    rp = sub.add_parser("report", help="汇总 report.json + report.md(纯本地)")
    rp.add_argument("--run-id", required=True)
    rp.set_defaults(func=cmd_report)

    r = sub.add_parser("run", help="build + ask(构建未通过则不提问;默认最后自动 purge)")
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
