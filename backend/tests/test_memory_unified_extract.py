import json
from types import SimpleNamespace
import pytest
from tests.test_memory_search import search_env, _seed, U1
from tests import memorykit
from app.memory import extract, unified_extract as unified, db, repo
from app.memory.errors import MemoryExtractionError

RAW="Ada and Lee reviewed the East report on Monday."
MESSAGES=[{"role":"user","message_id":"m1","recorded_at":"2024-06-04","content":RAW}]


def response():
    return {"facts":[{"text":"Ada reviewed a report.","kind":"event","source_message_ids":["m1"],
        "time_expression":"Monday","state":"completed","quotes":[{"message_id":"m1","quote":RAW}],
        "graph":{"entities":[{"ref":"a","name":"Ada","kind":"person","facts":[0]},
                              {"ref":"b","name":"Lee","kind":"person","facts":[0]}],
                 "relations":[{"subject":"a","relation":"reviewed","object_text":"East report","facts":[0],"quote":RAW}],
                 "events":[{"kind":"review","description_fact":0,"facts":[0],"participants":[
                     {"name":"Ada","role":"reviewer","entity_ref":"a"},
                     {"name":"Lee","role":"reviewer","entity_ref":"b"}]}]}}]}


def test_raw_graph_preserves_names_and_object_missing_from_summary(search_env):
    report=unified.parse(json.dumps(response()),MESSAGES)
    fact=report.facts[0]
    assert "Lee" not in fact.text and "East" not in fact.text
    assert fact.fact_context["time"]["raw"]=="Monday"
    stored=unified.stored_graph([SimpleNamespace(fact_context=fact.fact_context)])
    assert {e.name for e in stored.entities}=={"Ada","Lee"}
    assert stored.relations[0].object_text=="East report"
    assert stored.events[0].participants[1].name=="Lee"
    assert "Lee" in unified.precise_label(fact.fact_context) and "East report" in unified.precise_label(fact.fact_context)


def test_optional_attributes_null_is_rejected_without_losing_validation(search_env):
    data=response()
    data["facts"][0]["graph"]["events"][0]["attributes"]=None
    with pytest.raises(MemoryExtractionError,match="attributes"):
        unified.parse(json.dumps(data),MESSAGES)
    data["facts"][0]["graph"]["events"][0]["attributes"]={}
    assert unified.parse(json.dumps(data),MESSAGES).facts


@pytest.mark.parametrize("change",["unknown_source","assistant_source","invented_quote","invalid_ref"])
def test_invalid_original_grounding_or_structure_rejected(search_env,change):
    data=response()
    if change=="unknown_source":data["facts"][0]["quotes"][0]["message_id"]="missing"
    if change=="assistant_source":
        data["facts"][0]["source_message_ids"]=["assistant"]
        data["facts"][0]["quotes"][0]["message_id"]="assistant"
    if change=="invented_quote":data["facts"][0]["quotes"][0]["quote"]="invented"
    if change=="invalid_ref":data["facts"][0]["graph"]["entities"][0]["facts"]=[1]
    with pytest.raises(MemoryExtractionError):
        unified.parse(json.dumps(data),MESSAGES+[{"role":"assistant","message_id":"assistant","content":RAW}])


def test_one_call_and_staged_restore_preserve_precise_graph(search_env,monkeypatch):
    s=search_env.settings
    for k in ("memory_graph_enabled","memory_graph_write_enabled","memory_unified_extract_enabled"):
        monkeypatch.setattr(s,k,True)
    llm=memorykit.FakeLLM(json.dumps(response()))
    report=extract.extract_facts(MESSAGES,llm=llm,retries=0)
    assert report.attempts==1 and extract.protocol_version()==unified.PROTOCOL
    restored=extract.restore(extract.dump(report))
    assert restored.facts[0].fact_context==report.facts[0].fact_context
    assert unified.stored_graph([SimpleNamespace(fact_context=restored.facts[0].fact_context)]).events


def test_graph_master_off_keeps_legacy_extraction(search_env,monkeypatch):
    monkeypatch.setattr(search_env.settings,"memory_unified_extract_enabled",True)
    monkeypatch.setattr(search_env.settings,"memory_graph_enabled",False)
    report=extract.extract_facts(MESSAGES,llm=memorykit.FakeLLM('{"facts":[]}'))
    assert not report.facts and extract.protocol_version()==extract.PROTOCOL_VERSION


def test_untrusted_fact_text_cannot_enable_precision_system_instruction():
    from app.memory import maintain
    normal=maintain.build_messages("来源核验过的结构候选",[])
    trusted=maintain.build_messages("plain",[],precision=True)
    assert normal[0].content==maintain.SYSTEM_PROMPT
    assert trusted[0].content!=maintain.SYSTEM_PROMPT


def test_manual_edit_withdraws_stored_structural_evidence(search_env):
    mid=_seed(search_env,"Ada reviewed a report.")[0]
    context=unified.parse(json.dumps(response()),MESSAGES).facts[0].fact_context
    with db.session_scope() as session:
        repo.update_item(session,user_id=U1,memory_id=mid,new_text="Ada reviewed a report.",
            actor="llm",fact_context=context,now=db.utc_naive())
        repo.update_item(session,user_id=U1,memory_id=mid,new_text="Ada did not review it.",actor="user",now=db.utc_naive())
        assert "structured_graph" not in repo.get_item(session,U1,mid).fact_context
