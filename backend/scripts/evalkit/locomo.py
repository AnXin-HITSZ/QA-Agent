"""LoCoMo 原始数据 → 评测数据集(schema v2)的适配与校验(只做文件处理,不连库、不调模型)。

## 与官方数据的关系

数据来源:LoCoMo 基准(ACL 2024《Evaluating Very Long-Term Conversational Memory of
LLM Agents》,官方仓库 https://github.com/snap-research/locomo,README 与
`task_eval/` 源码;许可 CC BY-NC 4.0 —— 非商用)。本模块是**独立实现**的适配层,
未复制官方代码;官方语义(类别编号、evidence 形态、会话时间格式)以阅读官方
README 与源码为核对依据,出处与逐条差异见 `docs/LoCoMo长期记忆评测操作指南.md`。

## 原始结构(官方 `data/locomo10.json`,顶层是样本列表)

    [{
      "sample_id": "conv-26",
      "conversation": {
        "speaker_a": "Caroline", "speaker_b": "Melanie",
        "session_1": [{"speaker": "Caroline", "dia_id": "D1:1", "text": "…",
                        "img_url": "…", "blip_caption": "…", "query": "…"}, …],
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_2": [...], ...
      },
      "qa": [{"question": "…", "answer": "…", "category": 4,
              "evidence": ["D1:1", "D8:6; D9:17"]}, ...],
      "observation": {...}, "session_summary": {...}, "event_summary": {...}
    }, ...]

已知的数据形态(以 locomo10.json 实测,不是猜测):
- 会话键 `session_<n>` 的 `<n>` 是**数字序号**,排序必须按数值(`session_2` 早于
  `session_10`),不能按字符串;
- 个别样本存在**没有会话列表、只有 `session_<n>_date_time`** 的孤立日期键 → 记入
  备注并忽略,不造出空会话;
- 图片字段:官方不发布图片文件,只带 `img_url` / `blip_caption`(BLIP 生成的描述)/
  `query`;少数消息带 `re-download`。第一版**不下载、不识别图片**,只保留这些
  文本字段;历史文本里以 `[分享了图片,图片描述:…]` 附在消息之后(三种评测模式
  渲染一致);
- `qa` 里多数条目是 `answer`,不可回答类(类别 5)多数只有 `adversarial_answer`;
- `evidence` 条目可能一条里打包多个引用(`"D8:6; D9:17"`、`"D9:1 D4:4 D4:6"`)、
  带括号(`"(D2:1)"`)、补零(`"D30:05"`)或残缺(`"D"`、`"D:11:26"`)。适配时按
  `[;,\\s]+` 拆开、去括号、引用规范化为 `D<数字>:<数字>`(去前导零)后与消息对账;
  对不上的记入 `evidence_unresolved`(不丢弃、不报错——数据本身如此),全部对上记
  `ok`,部分对上记 `partial`,一条都对不上记 `unresolved`,没有引用记 `none`。

## 类别编号(以官方 `task_eval/evaluation.py` 的分派与注释为准,非记忆)

1=multi-hop, 2=temporal, 3=open-domain, 4=single-hop, 5=adversarial。
无法识别的类别**直接报错**(评分无法分派,不能默默当普通题处理)。

## 输出的评测数据集 schema v2

    {
      "schema_version": 2,
      "name": "locomo:locomo10.json",
      "source": {"kind": "locomo", "input_path": …, "input_sha256": …,
                 "prepared_at": …, "sample_limit": …, "attribution": …, "image_policy": …,
                 "notes": ["..."]},
      "conversations": [{
        "id": "locomo:conv-26", "sample_id": "conv-26",
        "subject": "locomo:conv-26",          # 一题一主体:样本的整段历史归一个评测用户
        "speakers": {"a": "Caroline", "b": "Melanie"},
        "sessions": [{"index": 1, "order": 1,
                      "date_time_raw": "1:56 pm on 8 May, 2023",
                      "date_time": "2023-05-08T13:56:00",     # 无时区信息 → 不带时区,不擅自补 UTC
                      "date_time_status": "parsed|unparsable|missing",
                      "turns": [{"dia_id": "D1:1", "speaker": "Caroline", "order": 1,
                                 "text": "…", "img_url": "", "blip_caption": "", "query": ""}]}]
      }],
      "questions": [{"id": "locomo:conv-26:q1", "conversation_id": "locomo:conv-26",
                     "sample_id": "conv-26", "question": "…", "answer": "…",
                     "adversarial_answer": "…", "category": 4, "category_name": "single-hop",
                     "evidence": ["D1:1"], "evidence_raw": ["D1:1"],
                     "evidence_unresolved": [], "evidence_status": "ok"}]
    }

保留但**不接入**的字段:`observation` / `session_summary` / `event_summary`(官方用于
RAG 对照与事件摘要任务,本次评测不使用,也不复制进产物,避免把任务外的标注带进构建)。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
CATEGORY_NAMES = {1: "multi-hop", 2: "temporal", 3: "open-domain",
                  4: "single-hop", 5: "adversarial"}

_SESSION_KEY = re.compile(r"^session_(\d+)$")
_DATE_KEY = re.compile(r"^session_(\d+)_date_time$")
_DATE_VALUE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*(am|pm)\s*on\s+(\d{1,2})\s+([A-Za-z]+),?\s*(\d{4})\s*$",
    re.IGNORECASE)
_DIA = re.compile(r"^D(\d+):(\d+)$")
_REF_SPLIT = re.compile(r"[;,\s]+")
_MONTHS = {name: i for i, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}

# 评测适配说明(随提取任务下发,见 service.enqueue_extraction 的 extract_note):
# 官方对话是**两个人**的聊天记录,而提取提示词面向「用户 ↔ 助手」的单用户会话。
# 映射方式:两位说话者的每条消息都映射为 user 角色、正文以「姓名:」开头(见
# render_turn)—— 不能把其中一人映射成 assistant,那等于把那个人的表述整段丢掉。
# 本条说明要求模型保留人物姓名、不许用「用户」笼统指代,避免两位人物的事实混成一人。
EXTRACT_NOTE_VERSION = "locomo-extract-note/2"
EXTRACT_NOTE = (
    "输入格式说明：这是两个人的对话记录，user 角色并不代表同一个人；"
    "每条正文前的姓名是说话者，请保留具体姓名，不用『用户』替代姓名。"
    "[会话记录时间:…] 和消息 ID 是来源元数据，不是人物说出的事实或事件时间。"
    "其余提取标准全部遵循共享提取器，不增加或放宽任何记忆选择规则。"
)



class LocomoDataError(ValueError):
    """原始 LoCoMo 数据不满足适配要求(结构/引用/类别等硬性错误)。"""


def canonical_dia(ref: str) -> str | None:
    """把一条 evidence 引用规范化成 `D<数字>:<数字>`(去前导零);不是该形态返回 None。"""
    m = _DIA.match((ref or "").strip())
    if not m:
        return None
    return f"D{int(m.group(1))}:{int(m.group(2))}"


def parse_session_time(raw: str) -> datetime | None:
    """解析官方的会话时间字符串(`1:56 pm on 8 May, 2023`)。

    只认这一种明确形态(含大小写与结尾逗号变体),解析失败返回 None(**不猜**)。
    结果是没有时区信息的朴素时间 —— 官方数据不带时区,这里也不擅自补 UTC。
    """
    m = _DATE_VALUE.match(raw or "")
    if not m:
        return None
    hour, minute, meridiem, day, month_name, year = m.groups()
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    hour = int(hour) % 12 + (12 if meridiem.lower() == "pm" else 0)
    try:
        return datetime(int(year), month, int(day), hour, int(minute))
    except ValueError:
        return None


# ---------------------------------------------------------------- 历史文本渲染(构建与全量上下文共用)


def render_turn(turn: dict) -> str:
    """一条消息的评测文本:`姓名: 正文`,图片以数据集自带的 BLIP 描述附后。

    构建(提取输入)与 full_context(全量上下文)用**同一函数**渲染,保证两种模式的
    历史内容逐字一致,只在「怎么用」上不同。图片只带描述文本(数据集自带,非本仓库
    生成),不下载、不识别。
    """
    line = f"{turn['speaker']}: {turn['text']}"
    caption = (turn.get("blip_caption") or "").strip()
    if caption:
        line += f" [分享了图片,图片描述:{caption}]"
    elif (turn.get("img_url") or "").strip():
        line += " [分享了图片(无描述)]"
    return line


def _session_time_note(session: dict) -> str:
    raw = (session.get("date_time_raw") or "").strip()
    iso = session.get("date_time") or ""
    if not raw:
        return "[会话记录时间:未知(数据集未提供)]"
    return f"[会话记录时间:{raw}]" if not iso else f"[会话记录时间:{raw}(标准时间:{iso})]"


def session_messages(session: dict) -> list[dict]:
    """把一个会话摊成提取任务的输入:时间元数据行 + 每条消息(全部 user 角色)。

    角色映射的取舍见模块 docstring 与 EXTRACT_NOTE:两位说话者都映射为 user,
    人物靠正文前缀的姓名区分;把其中一人映射成 assistant 会丢掉那个人的全部表述。
    """
    messages = [{"role": "user", "content": _session_time_note(session)}]
    for turn in session["turns"]:
        messages.append({"role": "user", "content": render_turn(turn),
                         "message_id": turn.get("dia_id") or turn.get("id") or "",
                         "speaker": turn.get("speaker") or "",
                         "recorded_at": session.get("date_time") or session.get("date_time_raw") or ""})
    return messages


def history_lines(conversation: dict) -> list[str]:
    """full_context 用的历史文本行:按会话分组、带时间与说话人,与构建输入同源渲染。"""
    lines: list[str] = []
    for session in conversation["sessions"]:
        when = session.get("date_time_raw") or "时间未知"
        lines.append(f"[会话 {session['index']}｜{when}]")
        for turn in session["turns"]:
            lines.append(render_turn(turn))
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


# ---------------------------------------------------------------- 适配


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _prepare_evidence(entries: Any, dia_set: set[str]) -> dict:
    """把一条 QA 的 evidence 原样保留并逐条对账(见模块 docstring 的形态说明)。"""
    raw_list = entries if isinstance(entries, list) else []
    resolved: list[str] = []
    unresolved: list[str] = []
    empty = 0
    for entry in raw_list:
        text = _text(entry)
        if not text:
            empty += 1
            continue
        text = text.replace("(", "").replace(")", "").strip()
        for part in _REF_SPLIT.split(text):
            part = part.strip()
            if not part:
                continue
            canon = canonical_dia(part)
            if canon and canon in dia_set:
                if canon not in resolved:
                    resolved.append(canon)
            else:
                unresolved.append(part)
    if not raw_list or (not resolved and not unresolved and empty):
        status = "none"
    elif unresolved and resolved:
        status = "partial"
    elif unresolved:
        status = "unresolved"
    else:
        status = "ok"
    return {"evidence": resolved, "evidence_raw": [str(e) for e in raw_list],
            "evidence_unresolved": unresolved, "evidence_status": status}


def _prepare_sample(sample: dict, *, index: int) -> tuple[dict, list[dict], dict]:
    """一个样本 → (conversation, questions, 计数备注)。结构错误抛 LocomoDataError。"""
    if not isinstance(sample, dict):
        raise LocomoDataError(f"第 {index} 个样本不是对象")
    sample_id = _text(sample.get("sample_id"))
    if not sample_id:
        raise LocomoDataError(f"第 {index} 个样本缺少 sample_id")
    conversation = sample.get("conversation")
    if not isinstance(conversation, dict):
        raise LocomoDataError(f"样本 {sample_id} 缺少 conversation 对象")
    speaker_a, speaker_b = _text(conversation.get("speaker_a")), _text(conversation.get("speaker_b"))
    if not speaker_a or not speaker_b or speaker_a == speaker_b:
        raise LocomoDataError(f"样本 {sample_id}:speaker_a / speaker_b 缺失或相同 —— "
                              "两位人物的身份必须可区分")

    # 会话:只认「有消息列表」的 session_<n>;孤立的时间键只记数(见模块 docstring)
    numbered: dict[int, list] = {}
    date_keys: dict[int, str] = {}
    for key, value in conversation.items():
        m = _SESSION_KEY.match(str(key))
        if m and isinstance(value, list):
            numbered[int(m.group(1))] = value
            continue
        m = _DATE_KEY.match(str(key))
        if m:
            date_keys[int(m.group(1))] = _text(value)
    orphan_dates = sorted(n for n in date_keys if n not in numbered)
    if not numbered:
        raise LocomoDataError(f"样本 {sample_id}:没有任何带消息列表的 session_<n>")

    sessions: list[dict] = []
    dia_set: set[str] = set()
    empty_sessions = 0
    for order, number in enumerate(sorted(numbered), start=1):
        turns_raw = numbered[number]
        if not turns_raw:
            empty_sessions += 1
            continue
        turns: list[dict] = []
        for pos, raw_turn in enumerate(turns_raw, start=1):
            if not isinstance(raw_turn, dict):
                raise LocomoDataError(f"样本 {sample_id} session_{number} 第 {pos} 条不是对象")
            dia_id = _text(raw_turn.get("dia_id"))
            speaker = _text(raw_turn.get("speaker"))
            text = _text(raw_turn.get("text"))
            if not dia_id:
                raise LocomoDataError(f"样本 {sample_id} session_{number} 第 {pos} 条缺少 dia_id")
            if speaker not in (speaker_a, speaker_b):
                raise LocomoDataError(f"样本 {sample_id} 的 {dia_id}:说话者 {speaker!r} "
                                      f"不是 speaker_a / speaker_b")
            if not text:
                raise LocomoDataError(f"样本 {sample_id} 的 {dia_id}:缺少 text")
            canon = canonical_dia(dia_id)
            if canon is None:
                raise LocomoDataError(f"样本 {sample_id}:dia_id {dia_id!r} 不是 D<数字>:<数字>")
            if canon in dia_set:
                raise LocomoDataError(f"样本 {sample_id}:dia_id {dia_id!r} 重复")
            dia_set.add(canon)
            turn = {"dia_id": dia_id, "speaker": speaker, "order": pos, "text": text,
                    "img_url": _text(raw_turn.get("img_url")),
                    "blip_caption": _text(raw_turn.get("blip_caption")),
                    "query": _text(raw_turn.get("query"))}
            turns.append(turn)
        raw_date = date_keys.get(number, "")
        parsed = parse_session_time(raw_date) if raw_date else None
        status = "missing" if not raw_date else ("parsed" if parsed else "unparsable")
        sessions.append({
            "index": number, "order": order,
            "date_time_raw": raw_date,
            "date_time": parsed.isoformat(timespec="seconds") if parsed else None,
            "date_time_status": status,
            "turns": turns,
        })
    if not sessions:
        raise LocomoDataError(f"样本 {sample_id}:所有会话都是空的")

    questions: list[dict] = []
    qa = sample.get("qa")
    if not isinstance(qa, list) or not qa:
        raise LocomoDataError(f"样本 {sample_id}:缺少 qa 列表")
    categories: dict[int, int] = {}
    evidence_unresolved_total = 0
    evidence_none = 0
    for q_index, item in enumerate(qa, start=1):
        if not isinstance(item, dict):
            raise LocomoDataError(f"样本 {sample_id} 第 {q_index} 条 QA 不是对象")
        question = _text(item.get("question"))
        if not question:
            raise LocomoDataError(f"样本 {sample_id} 第 {q_index} 条 QA 缺少 question")
        raw_category = item.get("category")
        if isinstance(raw_category, str) and raw_category.strip().isdigit():
            raw_category = int(raw_category.strip())
        if not isinstance(raw_category, int) or isinstance(raw_category, bool) \
                or raw_category not in CATEGORY_NAMES:
            raise LocomoDataError(f"样本 {sample_id} 第 {q_index} 条 QA 的类别无法识别:"
                                  f"{item.get('category')!r}(已知 {sorted(CATEGORY_NAMES)})")
        answer = item.get("answer")
        if isinstance(answer, (int, float)) and not isinstance(answer, bool):
            # 官方数据既有年份/数量等数字答案，也有文本答案；不能将数字误判为缺失。
            import math
            if not math.isfinite(answer):
                raise LocomoDataError(f"样本 {sample_id} 第 {q_index} 条 QA 的 answer 不是有限数字")
            answer = str(answer)
        elif answer is None:
            answer = ""
        elif not isinstance(answer, str):
            raise LocomoDataError(f"样本 {sample_id} 第 {q_index} 条 QA 的 answer 类型不受支持")
        adversarial = item.get("adversarial_answer")
        adversarial = adversarial if isinstance(adversarial, str) else ""
        if not answer.strip() and raw_category != 5:
            raise LocomoDataError(f"样本 {sample_id} 第 {q_index} 条 QA 既没有 answer,"
                                  "也不是不可回答类(类别 5)")
        ev = _prepare_evidence(item.get("evidence"), dia_set)
        evidence_unresolved_total += len(ev["evidence_unresolved"])
        if ev["evidence_status"] == "none":
            evidence_none += 1
        categories[raw_category] = categories.get(raw_category, 0) + 1
        questions.append({
            "id": f"locomo:{sample_id}:q{q_index}",
            "conversation_id": f"locomo:{sample_id}",
            "sample_id": sample_id,
            "question": question,
            "answer": answer,
            "adversarial_answer": adversarial,
            "category": raw_category,
            "category_name": CATEGORY_NAMES[raw_category],
            **ev,
        })

    conversation_out = {
        "id": f"locomo:{sample_id}",
        "sample_id": sample_id,
        "subject": f"locomo:{sample_id}",
        "speakers": {"a": speaker_a, "b": speaker_b},
        "sessions": sessions,
    }
    notes = {"empty_sessions": empty_sessions, "orphan_dates": orphan_dates,
             "categories": categories, "evidence_unresolved": evidence_unresolved_total,
             "evidence_none": evidence_none,
             "unparsable_dates": sum(1 for s in sessions if s["date_time_status"] == "unparsable")}
    return conversation_out, questions, notes


def prepare(raw: Any, *, input_path: str = "", sample_limit: int | None = None) -> dict:
    """原始 LoCoMo 数据(已 json.loads 的顶层列表)→ 评测数据集(schema v2)。

    硬性错误(样本/消息/问题结构、说话者、类别、dia_id)直接抛 LocomoDataError ——
    「宁可停,不带半份数据往下跑」。可容忍的数据形态(evidence 对不上、时间缺失、
    空会话、孤立日期键)如实记录进 `source.notes`。
    """
    if not isinstance(raw, list) or not raw:
        raise LocomoDataError("原始数据顶层应是非空列表(官方 locomo10.json 的结构)")
    if sample_limit is not None and sample_limit < 1:
        raise LocomoDataError("sample-limit 必须是正整数(不传=全量)")
    samples = raw[:sample_limit] if sample_limit else raw

    conversations: list[dict] = []
    questions: list[dict] = []
    seen_ids: set[str] = set()
    totals = {"sessions": 0, "turns": 0, "captions": 0, "images": 0,
              "empty_sessions": 0, "orphan_dates": 0, "evidence_unresolved": 0,
              "evidence_none": 0, "unparsable_dates": 0, "missing_dates": 0}
    categories: dict[int, int] = {}
    unresolved_examples: list[str] = []
    orphan_examples: list[str] = []

    for index, sample in enumerate(samples, start=1):
        conversation, sample_questions, notes = _prepare_sample(sample, index=index)
        sample_id = conversation["sample_id"]
        if sample_id in seen_ids:
            raise LocomoDataError(f"sample_id 重复:{sample_id}")
        seen_ids.add(sample_id)
        conversations.append(conversation)
        questions.extend(sample_questions)
        totals["sessions"] += len(conversation["sessions"])
        totals["turns"] += sum(len(s["turns"]) for s in conversation["sessions"])
        totals["captions"] += sum(1 for s in conversation["sessions"] for t in s["turns"]
                                  if t["blip_caption"])
        totals["images"] += sum(1 for s in conversation["sessions"] for t in s["turns"]
                                if t["img_url"])
        totals["empty_sessions"] += notes["empty_sessions"]
        totals["orphan_dates"] += len(notes["orphan_dates"])
        if notes["orphan_dates"] and len(orphan_examples) < 5:
            orphan_examples.append(f"{sample_id}: session_{notes['orphan_dates'][0]}_date_time")
        totals["evidence_unresolved"] += notes["evidence_unresolved"]
        totals["evidence_none"] += notes["evidence_none"]
        totals["unparsable_dates"] += notes["unparsable_dates"]
        totals["missing_dates"] += sum(1 for s in conversation["sessions"]
                                       if s["date_time_status"] == "missing")
        for key, value in notes["categories"].items():
            categories[key] = categories.get(key, 0) + value
    # 未对上的 evidence 里挑几条原样举例(便于人工核对是不是数据自身形态)
    for q in questions:
        for ref in q["evidence_unresolved"]:
            if len(unresolved_examples) < 5 and ref not in unresolved_examples:
                unresolved_examples.append(ref)

    source = {
        "kind": "locomo",
        "input_path": str(Path(input_path).resolve()) if input_path else "",
        "input_sha256": hashlib.sha256(
            json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sample_limit": sample_limit,
        "attribution": ("数据来自 LoCoMo 基准(ACL 2024, snap-research/locomo, CC BY-NC 4.0,"
                        " 仅限非商用);本文件是独立实现的结构适配产物,未复制官方代码。"),
        "image_policy": ("不下载、不识别图片:img_url / blip_caption(数据集自带的 BLIP 描述)/"
                         " query 原样保留在 turns 里;历史文本以 [分享了图片,图片描述:…] 附在"
                         "消息之后,三种评测模式渲染一致。"),
        "notes": [
            f"样本 {len(conversations)} 个,会话 {totals['sessions']} 段,消息 {totals['turns']} 条,"
            f"问题 {len(questions)} 条",
            "类别分布:" + ", ".join(f"{c}={CATEGORY_NAMES[c]}:{n}"
                                    for c, n in sorted(categories.items())),
            f"带图片描述的消息 {totals['captions']} 条(带 img_url {totals['images']} 条),未下载图片",
            f"evidence:未对上引用 {totals['evidence_unresolved']} 条,无引用/全空 {totals['evidence_none']} 条"
            + (f";示例:{unresolved_examples}" if unresolved_examples else ""),
            f"空会话(列表为空)跳过 {totals['empty_sessions']} 段;孤立日期键忽略 {totals['orphan_dates']} 个"
            + (f";示例:{orphan_examples}" if orphan_examples else ""),
            f"会话时间:缺失 {totals['missing_dates']} 段,无法解析 {totals['unparsable_dates']} 段"
            "(原始字符串照留,不猜)",
            "未接入 observation / session_summary / event_summary(任务外标注,不复制进产物)",
        ],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "name": f"locomo:{Path(input_path).name}" if input_path else "locomo(prepared)",
        "source": source,
        "conversations": conversations,
        "questions": questions,
    }


def prepare_file(input_path: str, output_path: str, *, sample_limit: int | None = None) -> dict:
    """读原始文件 → 适配 → 写评测数据集文件;返回数据集本身(含 source 备注)。"""
    raw = json.loads(Path(input_path).read_text(encoding="utf-8"))
    dataset = prepare(raw, input_path=input_path, sample_limit=sample_limit)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    return dataset
