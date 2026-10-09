"""Conservative event-time normalization; never substitute ingestion time."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from app.memory.errors import MemoryExtractionError


def resolve_time(raw: str, anchor: str = "") -> dict:
    result = {"raw": raw, "anchor": anchor, "start": None, "end": None,
              "precision": "unknown", "status": "unknown"}
    raw = raw.strip()
    if not raw:
        return result
    # Date ranges have independently specified endpoints; no missing year inference.
    parts = re.split(r"\s+(?:to|至)\s+|至|/", raw)
    if len(parts) == 2:
        left, right = resolve_time(parts[0]), resolve_time(parts[1])
        if left["start"] and right["start"] and left["start"] <= right["start"]:
            result.update(start=left["start"], end=right["start"], precision="range", status="parsed")
            return result
    match = re.fullmatch(r"(\d{4})(?:年|-)(\d{1,2})(?:(?:月|-)(\d{1,2})日?|月)?", raw)
    months = {name: i for i, name in enumerate(("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1)}
    english_month = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", raw)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?", raw):
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            precision = "second" if re.match(r".*[T ]\d{2}:\d{2}:\d{2}", raw) else "minute"
            result.update(start=stamp.isoformat(timespec="seconds" if precision == "second" else "minutes"),
                          precision=precision, status="parsed")
        elif re.fullmatch(r"\d{4}年?", raw):
            year = int(raw.rstrip("年"))
            datetime(year, 1, 1)
            result.update(start=f"{year:04d}", precision="year", status="parsed")
        elif match:
            year, month, day = match.groups()
            datetime(int(year), int(month), int(day or 1))
            value = f"{int(year):04d}-{int(month):02d}" + (f"-{int(day):02d}" if day else "")
            result.update(start=value, precision="day" if day else "month", status="parsed")
        elif english_month and english_month[1].lower() in months:
            year = int(english_month[2])
            month = months[english_month[1].lower()]
            datetime(year, month, 1)
            result.update(start=f"{year:04d}-{month:02d}", precision="month", status="parsed")
        elif raw.lower() in ("昨天", "今天", "明天", "yesterday", "today", "tomorrow", "上个月", "下个月", "last month", "next month", "去年", "明年", "last year", "next year"):
            if not anchor:
                result["status"] = "unanchored"
                return result
            base = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
            word = raw.lower()
            days = {"昨天": -1, "yesterday": -1, "今天": 0, "today": 0, "明天": 1, "tomorrow": 1}
            if word in days:
                result.update(start=(base + timedelta(days=days[word])).strftime("%Y-%m-%d"), precision="day")
            elif word in ("上个月", "下个月", "last month", "next month"):
                offset = -1 if word in ("上个月", "last month") else 1
                index = base.year * 12 + base.month - 1 + offset
                result.update(start=f"{index // 12:04d}-{index % 12 + 1:02d}", precision="month")
            else:
                offset = -1 if word in ("去年", "last year") else 1
                result.update(start=f"{base.year + offset:04d}", precision="year")
            result["status"] = "resolved"
        else:
            result["status"] = "ambiguous"
    except (ValueError, OverflowError):
        result["status"] = "invalid"
    return result


def enrich_facts(facts, messages):
    sources = {str(m.get("message_id") or f"m{i}"): m for i, m in enumerate(messages)
               if m.get("role") == "user"}
    for fact in facts:
        ids = fact.source_message_ids
        if any(mid not in sources for mid in ids):
            raise MemoryExtractionError("来源消息标识不存在或不是用户消息")
        if not ids and len(sources) == 1:
            ids = list(sources)
        if not ids and fact.kind in ("event", "task") and len(sources) > 1:
            raise MemoryExtractionError("事件或计划必须引用来源消息标识")
        selected = [sources[mid] for mid in ids]
        anchors = {str(m.get("recorded_at") or "") for m in selected}
        anchor = next(iter(anchors)) if len(anchors) == 1 else ""
        # Unverifiable temporal expressions are retained but cannot resolve a date.
        raw = fact.time_expression
        temporal = resolve_time(raw, anchor)
        if raw and not any(raw.casefold() in str(m.get("content", "")).casefold() for m in selected):
            temporal.update(start=None, end=None, precision="unknown", status="unverified")
        if not raw and fact.event_time:
            # A legacy explicit timestamp must not be rounded back to midnight.
            value = fact.event_time.isoformat(timespec="seconds")
            if fact.event_time.time().isoformat() == "00:00:00":
                value = fact.event_time.strftime("%Y-%m-%d")
            temporal = resolve_time(value)
        fact.fact_context = {"time": temporal, "state": fact.state,
                             "correction_evidence": any(re.search(r"更正|纠正|之前.{0,12}(说错|不准确|有误)|I (?:was wrong|misspoke)|correction:|correct what I", str(m.get("content", "")), re.I) for m in selected),
                             "sources": [{"message_id": mid, "speaker": sources[mid].get("speaker", ""),
                                          "recorded_at": sources[mid].get("recorded_at", "")} for mid in ids]}
        if temporal["precision"] in ("day", "minute", "second") and temporal["start"]:
            from app.memory.models import parse_event_time
            fact.event_time = parse_event_time(temporal["start"])
        elif raw:
            fact.event_time = None  # year/month/range must not fabricate a day
        if fact.kind in ("event", "task") and temporal.get("start"):
            date = temporal["start"] + (f" to {temporal['end']}" if temporal.get("end") else "")
            if date not in fact.text:
                fact.text += f" [{date}]"


def context_label(context: dict) -> str:
    if not context:
        return ""
    time = context.get("time") or {}
    labels = [f"事件状态:{context.get('state', 'unknown')}"]
    if time.get("raw"):
        labels.append(f"时间原文:{time['raw']}")
    if time.get("start"):
        labels.append(f"事件时间:{time['start']}" + (f"至{time['end']}" if time.get("end") else ""))
    labels.append(f"时间精度:{time.get('precision', 'unknown')};解析:{time.get('status', 'unknown')}")
    for source in context.get("sources", []):
        labels.append(f"来源消息:{source.get('message_id', '')};说话者:{source.get('speaker', '')};记录时间:{source.get('recorded_at') or '未知'}")
    return "\n[" + "；".join(labels) + "]"
