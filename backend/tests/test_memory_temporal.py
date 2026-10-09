"""Shared Chinese/English memory behavior; offline, no provider calls."""
import json
from datetime import datetime

import pytest

from app.memory import extract, maintain, service, search, repo
from app.memory.temporal import resolve_time, enrich_facts, context_label


def test_unresolved_relative_time_keeps_anchor_and_precision_limit():
    label = context_label({"time": resolve_time("last week", "2023-06-09T19:55:00")})
    assert "last week" in label and "2023-06-09T19:55:00" in label
    assert "不等于没有时间证据" in label
    assert "不得补造具体某一天" in label
    assert resolve_time("last week", "2023-06-09T19:55:00")["start"] is None
from app.memory.errors import MemoryExtractionError
from app.memory.errors import MemoryConflict
from app.memory.models import ACTOR_USER
from tests.test_memory_extract import FakeLLM
from tests.test_memory_service import mem_env, U1


@pytest.mark.parametrize("raw,anchor,start,precision", [
    ("昨天", "2023-05-08T13:56:00", "2023-05-07", "day"),
    ("yesterday", "2023-05-08T13:56:00", "2023-05-07", "day"),
    ("上个月", "2023-01-08T13:56:00", "2022-12", "month"),
    ("明年", "2023-05-08T13:56:00", "2024", "year"),
    ("2022", "", "2022", "year"),
    ("2023年6月", "", "2023-06", "month"),
    ("June 2023", "", "2023-06", "month"),
    ("2023-05-07", "", "2023-05-07", "day"),
    ("2023-05-07T13:45", "", "2023-05-07T13:45", "minute"),
    ("2023-05-07至2023-05-09", "", "2023-05-07", "range"),
])
def test_precision_and_trusted_anchor(raw, anchor, start, precision):
    time = resolve_time(raw, anchor)
    assert time["start"] == start and time["precision"] == precision


@pytest.mark.parametrize("raw,anchor,status", [
    ("昨天", "", "unanchored"), ("昨天", "not a date", "invalid"),
    ("2023-02-30", "", "invalid"), ("recently", "", "ambiguous"),
])
def test_unknown_times_never_use_today(raw, anchor, status):
    result = resolve_time(raw, anchor)
    assert result["start"] is None and result["status"] == status


def messages(content="小安：昨天完成了实验", stamp="2023-05-08T13:56:00"):
    return [{"role": "user", "message_id": "m1", "speaker": "小安",
             "recorded_at": stamp, "content": content}]


def test_event_enrichment_preserves_language_and_source():
    fact = extract.ExtractedFact(text="小安完成了实验", kind="event", time_expression="昨天",
                                 source_message_ids=["m1"], state="completed")
    enrich_facts([fact], messages())
    assert fact.event_time == datetime(2023, 5, 7)
    assert fact.text == "小安完成了实验 [2023-05-07]"
    assert fact.fact_context["sources"][0]["speaker"] == "小安"
    assert fact.fact_context["time"]["raw"] == "昨天"
    assert extract.restore(extract.dump(extract.ExtractionReport(facts=[fact]))).facts[0] == fact


def test_month_does_not_fabricate_first_day():
    fact = extract.ExtractedFact(text="小安计划参加会议", kind="task", time_expression="2023年6月")
    enrich_facts([fact], messages("小安：2023年6月计划参加会议"))
    assert fact.event_time is None
    assert fact.fact_context["time"]["start"] == "2023-06"


def test_explicit_timestamp_is_not_rounded_to_midnight():
    fact = extract.ExtractedFact(text="小安完成实验", kind="event", event_time="2023-05-07T13:45:00")
    enrich_facts([fact], messages())
    assert fact.event_time == datetime(2023, 5, 7, 13, 45)
    assert fact.fact_context["time"]["precision"] == "second"


def test_legacy_relative_date_is_preserved_and_grounded():
    llm = FakeLLM(json.dumps({"facts": [{"text": "小安完成实验", "kind": "event", "event_time": "昨天"}]}))
    fact = extract.extract_facts(messages(), llm=llm).facts[0]
    assert fact.event_time == datetime(2023, 5, 7)
    assert fact.fact_context["time"]["raw"] == "昨天"


def test_unknown_source_is_retried_and_assistant_cannot_supply_evidence():
    invalid = {"text": "小安完成了实验", "kind": "event", "source_message_ids": ["assistant"]}
    valid = {"text": "小安完成了实验", "kind": "event", "source_message_ids": ["m1"]}
    llm = FakeLLM(json.dumps({"facts": [invalid]}), json.dumps({"facts": [valid]}))
    result = extract.extract_facts([*messages(), {"role": "assistant", "message_id": "assistant", "content": "建议"}], llm=llm, retries=1)
    assert result.attempts == 2


def test_model_cannot_forge_backend_context():
    with pytest.raises(MemoryExtractionError):
        extract._parse(json.dumps({"facts": [{"text": "假事实", "kind": "event",
                                             "fact_context": {"correction_evidence": True}}]}), limit=4000)


def test_same_sentence_different_dates_remains_two_events(mem_env):
    facts = []
    for stamp in ("2023-05-08", "2023-05-09"):
        fact = extract.ExtractedFact(text="小安完成实验", kind="event", time_expression="昨天")
        enrich_facts([fact], messages(stamp=stamp))
        facts.append(fact)
    result = service.write_facts(user_id=U1, facts=facts)
    assert len(result.outcome.added) == 2
    assert {item.fact_context["time"]["start"] for item in result.outcome.added} == {"2023-05-07", "2023-05-08"}


def test_progress_cannot_overwrite_plan():
    cand = maintain.Candidate("abcd", "小安计划参加会议", 1, kind="task")
    fact = extract.ExtractedFact(text="小安已经参加会议", kind="event", state="completed")
    decision = maintain.MaintenanceDecision(events=[maintain.UpdateEvent(event="UPDATE", id="abcd", text=fact.text)])
    guarded = service._protect_history(decision, [cand], fact)
    assert isinstance(guarded.events[0], maintain.AddEvent)
    assert guarded.events[0].text == fact.text


def test_correction_flag_requires_user_evidence():
    cand = maintain.Candidate("abcd", "小安参加会议", 1, kind="event")
    fact = extract.ExtractedFact(text="小安没有参加会议", kind="event")
    decision = maintain.MaintenanceDecision(events=[maintain.DeleteEvent(event="DELETE", id="abcd", correction=True)])
    assert isinstance(service._protect_history(decision, [cand], fact).events[0], maintain.AddEvent)
    enrich_facts([fact], messages("更正：之前说错了，小安没有参加会议"))
    assert service._protect_history(decision, [cand], fact) is decision


def test_noop_cannot_discard_a_distinct_event_date():
    cand = maintain.Candidate("abcd", "小安完成实验", 1, kind="event",
                              fact_context={"time": {"start": "2023-05-06"}})
    fact = extract.ExtractedFact(text="小安完成实验", kind="event", time_expression="昨天")
    enrich_facts([fact], messages())
    decision = maintain.MaintenanceDecision(events=[maintain.NoneEvent(event="NONE")])
    assert isinstance(service._protect_history(decision, [cand], fact).events[0], maintain.AddEvent)


def test_metadata_update_reuses_vector_and_scrubs_context_on_purge(mem_env):
    result = service.write_facts(user_id=U1, facts=[extract.ExtractedFact(text="用户偏好中文", kind="preference")])
    mid = result.outcome.added[0].id
    with mem_env.db.session_scope() as session:
        current = repo.get_item(session, U1, mid)
        updated = repo.update_item(session, user_id=U1, memory_id=mid, new_text=current.text,
                                   actor=ACTOR_USER, now=datetime(2026, 10, 9),
                                   fact_context={"state": "unknown"})
        assert updated.revision == current.revision and updated.meta_version == current.meta_version + 1
        assert updated.vector_is_current(service.embedding_version())
    with mem_env.db.session_scope() as session:
        repo.purge_user(session, U1, now=datetime(2026, 10, 9))
        history = repo.list_history(session, U1)
        assert all(h.old_context is None and h.new_context is None for h in history)


def test_shared_prompt_and_eval_answer_boundaries():
    from scripts.memory_eval import EVAL_ANSWER_INSTRUCTION
    assert "Always answer in English" in EVAL_ANSWER_INSTRUCTION
    assert "LoCoMo" not in extract.SYSTEM_PROMPT
    assert "SOP" in extract.SYSTEM_PROMPT and "助手建议" in extract.SYSTEM_PROMPT


def test_stale_metadata_cannot_be_overwritten_with_same_revision(mem_env):
    result = service.write_facts(user_id=U1, facts=[extract.ExtractedFact(text="用户喜欢中文", kind="preference")])
    mid = result.outcome.added[0].id
    with mem_env.db.session_scope() as session:
        before = repo.get_item(session, U1, mid)
        repo.update_item(session, user_id=U1, memory_id=mid, new_text=before.text,
                         actor=ACTOR_USER, now=datetime(2026, 10, 9), fact_context={"state": "ongoing"})
    with pytest.raises(MemoryConflict), mem_env.db.session_scope() as session:
        repo.update_item(session, user_id=U1, memory_id=mid, new_text=before.text,
                         actor=ACTOR_USER, now=datetime(2026, 10, 9), fact_context={},
                         expected_revision=before.revision, expected_meta_version=before.meta_version)
