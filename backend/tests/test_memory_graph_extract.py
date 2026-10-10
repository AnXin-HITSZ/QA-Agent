"""图提取协议（mem-graph-extract/1）:内容出处校验 / 逐项丢弃 / 有限重试 / 提示词口径。

全部离线:模型是假的(按脚本吐字符串),不触网、不计费。核心看护两点:
- **一致性靠构造**:图元素必须引用事实编号,名称 / 宾语 / 参与者 / 属性值必须能在
  被引用事实(实体名:全批事实)正文里逐字找到 —— 找不到就丢弃并记原因;
- **结构错误(JSON / 编号越界 / 枚举外)** 整份重试,与 mem-extract/5 同一纪律。
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from app.memory import extract
from app.memory.errors import MemoryExtractionError
from app.memory.graph import extract as graph_extract


class FakeLLM:
    """按脚本返回内容的假模型:str → AIMessage;异常实例 → 直接抛(模拟网络错误)。"""

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.prompts: list[list] = []

    def invoke(self, messages, **kwargs):
        self.prompts.append(list(messages))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            # Fixture protocol v2 includes source quotes. Recover the exact fact
            # text from the supplied prompt instead of inventing an example.
            import re
            try:
                value = json.loads(item)
                sources = {int(i): text for i, text in re.findall(
                    r"\[事实(\d+)\]\([^\n]*?\) (.*)", str(messages[-1].content))}
                if isinstance(value, dict):
                    for relation in value.get("relations", []):
                        relation.setdefault("quote", sources.get(relation.get("facts", [0])[0], "invalid source"))
                    item = json.dumps(value, ensure_ascii=False)
            except (ValueError, IndexError, AttributeError, TypeError):
                pass
            return AIMessage(content=item)
        return item


def _fact(text, kind="profile", state="unknown", time_expression="", recorded=""):
    context = {"sources": [{"recorded_at": recorded}]} if recorded else {}
    return extract.ExtractedFact(text=text, kind=kind, state=state,
                                 time_expression=time_expression, fact_context=context)


def _payload(entities=(), relations=(), events=()) -> str:
    from app.memory.graph.models import name_key
    entities = [{"ref": e.get("ref", name_key(e["name"])), **e} for e in entities]
    return json.dumps({"entities": list(entities), "relations": list(relations),
                       "events": list(events)}, ensure_ascii=False)


MSGS = [{"role": "user", "content": "Caroline 说她换了工作,还养了只猫"}]


# ---- 正常解析 ----


def test_extracts_entities_relations_events():
    facts = [_fact("Caroline has a cat named Rex"), _fact("Caroline works at Google")]
    llm = FakeLLM(_payload(
        entities=[{"name": "Caroline", "kind": "person", "aliases": [], "facts": [0, 1]},
                  {"name": "Rex", "kind": "animal", "aliases": [], "facts": [0]}],
        relations=[{"subject": "Caroline", "relation": "has_pet", "object_entity": "Rex",
                    "object_text": "", "facts": [0]},
                   {"subject": "Caroline", "relation": "works_at", "object_entity": "",
                    "object_text": "Google", "facts": [1]}],
        events=[{"kind": "change", "description_fact": 1, "facts": [1],
                 "participants": [{"name": "Caroline", "role": "subject"}], "attributes": {}}],
    ))
    report = graph_extract.extract_graph(facts, MSGS, llm=llm, retries=0)

    assert [e.name for e in report.entities] == ["Caroline", "Rex"]
    assert report.relations[0].object_entity == report.entities[1].ref and report.relations[0].object_text == ""
    assert report.relations[1].object_text == "Google"
    assert report.events[0].description_fact == 1 and report.dropped == []
    assert report.attempts == 1


def test_fact_lines_and_conversation_go_into_the_prompt():
    facts = [_fact("Caroline 在写论文", recorded="2026-10-01T08:00:00")]
    llm = FakeLLM(_payload())
    graph_extract.extract_graph(facts, MSGS, llm=llm, retries=0, note="评测专用说明")
    prompt = llm.prompts[0]

    assert isinstance(prompt[0], SystemMessage)
    assert isinstance(prompt[1], SystemMessage) and prompt[1].content == "评测专用说明"
    body = prompt[-1].content
    assert "[事实0]" in body and "Caroline 在写论文" in body and "记录时间:2026-10-01T08:00:00" in body
    assert "消息ID" in body                       # 对话节选在(理解指代用)


def test_empty_result_is_normal_and_empty_facts_skip_the_call():
    llm = FakeLLM('```json\n{"entities": [], "relations": [], "events": []}\n```')
    report = graph_extract.extract_graph([_fact("x 在写论文")], MSGS, llm=llm, retries=1)
    assert (report.entities, report.relations, report.events, report.dropped) == ([], [], [], [])
    assert report.attempts == 1 and len(llm.prompts) == 1

    quiet = FakeLLM()
    report = graph_extract.extract_graph([], MSGS, llm=quiet, retries=1)
    assert quiet.prompts == [] and report.attempts == 0      # 没有事实:不调用模型


# ---- 内容出处(一致性靠构造) ----


def test_entity_name_must_appear_in_the_facts():
    facts = [_fact("用户在实验室做表征")]
    llm = FakeLLM(_payload(entities=[{"name": "张三", "kind": "person", "facts": [0]}]))
    report = graph_extract.extract_graph(facts, MSGS, llm=llm, retries=0)
    assert report.entities == []
    assert any("实体名在事实正文里找不到" in r for r in report.dropped)


def test_pronoun_resolution_grounds_against_the_whole_batch():
    """指代消解:名字出现在**别的事实**里也算数(跨消息引用);引用编号记录出处。"""
    facts = [_fact("She got a new job at Google"), _fact("Caroline is my sister")]
    llm = FakeLLM(_payload(
        entities=[{"name": "Caroline", "kind": "person", "facts": [0]}],
        relations=[{"subject": "Caroline", "relation": "works_at", "object_entity": "",
                    "object_text": "Google", "facts": [0]}],
    ))
    report = graph_extract.extract_graph(facts, MSGS, llm=llm, retries=0)
    assert [e.name for e in report.entities] == ["Caroline"]
    assert not report.relations  # Unrelated name occurrence cannot prove this pronoun's referent.


def test_relation_object_text_must_appear_in_its_cited_facts():
    facts = [_fact("Caroline has a cat named Rex"), _fact("Caroline lives in Shenzhen")]
    entities = [{"name": "Caroline", "kind": "person", "facts": [0, 1]}]
    bad = FakeLLM(_payload(entities=entities, relations=[
        {"subject": "Caroline", "relation": "lives_in", "object_entity": "",
         "object_text": "Shenzhen", "facts": [0]}]))          # 引错了事实编号
    report = graph_extract.extract_graph(facts, MSGS, llm=bad, retries=0)
    assert report.relations == []
    assert any("宾语短语在被引用事实里找不到" in r for r in report.dropped)

    good = FakeLLM(_payload(entities=entities, relations=[
        {"subject": "Caroline", "relation": "lives_in", "object_entity": "",
         "object_text": "Shenzhen", "facts": [1]}]))
    report = graph_extract.extract_graph(facts, MSGS, llm=good, retries=0)
    assert len(report.relations) == 1


def test_relation_object_needs_exactly_one_form():
    facts = [_fact("Caroline has a cat named Rex")]
    entities = [{"name": "Caroline", "kind": "person", "facts": [0]},
                {"name": "Rex", "kind": "animal", "facts": [0]}]
    llm = FakeLLM(_payload(entities=entities, relations=[
        {"subject": "Caroline", "relation": "has_pet", "object_entity": "Rex",
         "object_text": "a cat", "facts": [0]},          # 两种都给了
        {"subject": "Caroline", "relation": "related_to", "object_entity": "",
         "object_text": "", "facts": [0]}]))             # 什么都没给
    report = graph_extract.extract_graph(facts, MSGS, llm=llm, retries=0)
    assert report.relations == []
    reasons = "；".join(report.dropped)
    assert "二选一" in reasons and "缺少宾语" in reasons


def test_relation_ends_must_be_declared_entities():
    facts = [_fact("Caroline has a cat named Rex")]
    llm = FakeLLM(_payload(relations=[
        {"subject": "Caroline", "relation": "has_pet", "object_entity": "",
         "object_text": "a cat", "facts": [0]}]))       # 没声明实体就引用
    report = graph_extract.extract_graph(facts, MSGS, llm=llm, retries=0)
    assert report.relations == []
    assert any("主语不是已声明的实体" in r for r in report.dropped)


def test_duplicate_relations_within_response_are_dropped():
    facts = [_fact("Caroline has a cat named Rex")]
    entities = [{"name": "Caroline", "kind": "person", "facts": [0]}]
    llm = FakeLLM(_payload(entities=entities, relations=[
        {"subject": "Caroline", "relation": "has_pet", "object_entity": "",
         "object_text": "a cat", "facts": [0]},
        {"subject": "caroline", "relation": "has_pet", "object_entity": "",
         "object_text": "A CAT", "facts": [0]}]))       # 大小写不同,同一关系
    report = graph_extract.extract_graph(facts, MSGS, llm=llm, retries=0)
    assert len(report.relations) == 1
    assert any("已合并来源" in r for r in report.dropped)


# ---- 别名 ----


def test_aliases_grounded_deduped_and_conflict_checked():
    facts = [_fact("Caroline goes by Carol"), _fact("Bobby joined the call")]
    llm = FakeLLM(_payload(entities=[
        {"name": "Caroline", "kind": "person", "aliases": ["Carol", "Lina", "Bobby"],
         "facts": [0]},                                   # Lina 不在事实里;Bobby 是别人的名字
        {"name": "Bobby", "kind": "person", "aliases": ["Carol"], "facts": [1]},
    ]))                                                   # Carol 已挂在 Caroline 上
    report = graph_extract.extract_graph(facts, MSGS, llm=llm, retries=0)
    caroline, bobby = report.entities
    assert caroline.aliases == ["Carol"]
    assert bobby.aliases == []
    reasons = "；".join(report.dropped)
    assert "别名在事实正文里找不到" in reasons
    assert "别名与另一实体的规范名相同" in reasons
    assert "别名已挂在实体" in reasons


def test_same_name_entities_merge_within_one_response():
    """同一响应里的同名条目无法区分,按同一实体合并(不是「同名≠同一实体」的例外:
    同名不同实体靠**跨批次的消歧**处理,这里合并的只是同一次输出的重复写法)。"""
    facts = [_fact("Caroline goes by Carol"), _fact("Caroline works at Google")]
    llm = FakeLLM(_payload(entities=[
        {"name": "Caroline", "kind": "person", "aliases": ["Carol"], "facts": [0]},
        {"name": "caroline", "kind": "person", "aliases": [], "facts": [1]},
    ]))
    report = graph_extract.extract_graph(facts, MSGS, llm=llm, retries=0)
    assert len(report.entities) == 1
    assert report.entities[0].facts == [0, 1]
    assert report.entities[0].aliases == ["Carol"]


# ---- 事件 ----


def test_event_description_fact_must_be_cited():
    facts = [_fact("打算下周去北京", kind="task", state="planned"),
             _fact("下周去北京是确定的")]
    good = FakeLLM(_payload(events=[{"kind": "trip", "description_fact": 0, "facts": [0],
                                     "participants": [], "attributes": {}}]))
    report = graph_extract.extract_graph(facts, MSGS, llm=good, retries=0)
    assert len(report.events) == 1

    # 编号越界:结构错误,整份重试后失败
    overflow = _payload(events=[{"kind": "trip", "description_fact": 9, "facts": [0]}])
    with pytest.raises(MemoryExtractionError):
        graph_extract.extract_graph(facts, MSGS, llm=FakeLLM(overflow), retries=0)

    # description_fact 合法但不在 facts 里:丢弃该事件,不整份重试
    slip = FakeLLM(_payload(events=[{"kind": "trip", "description_fact": 1, "facts": [0]}]))
    report = graph_extract.extract_graph(facts, MSGS, llm=slip, retries=0)
    assert report.events == []
    assert any("描述来源不在它引用的编号里" in r for r in report.dropped)


def test_event_participants_and_attributes_must_be_grounded():
    facts = [_fact("Caroline and Bob visited the Louvre in 2024", time_expression="2024")]
    llm = FakeLLM(_payload(
        entities=[{"name": "Caroline", "kind": "person", "facts": [0]}],
        events=[{"kind": "trip", "description_fact": 0, "facts": [0],
                 "participants": [{"name": "Caroline", "role": "subject"},
                                  {"name": "Alice", "role": "companion"}],   # 事实里没有
                 "attributes": {"地点": "Louvre", "金额": "一百元"}}]))       # 金额没依据
    report = graph_extract.extract_graph(facts, MSGS, llm=llm, retries=0)
    event = report.events[0]
    assert [p.name for p in event.participants] == ["Caroline"]
    assert event.attributes == {"地点": "Louvre"}
    reasons = "；".join(report.dropped)
    assert "参与者「Alice」" in reasons and "属性「金额」" in reasons


def test_repeated_mentions_of_one_event_merge():
    """同一次事件的重复提及合并成一条(计数题的去重依据);参与者与来源取并集。"""
    facts = [_fact("Caroline went to Beijing last summer", kind="event"),
             _fact("Caroline and Bob visited Beijing again", kind="event")]
    llm = FakeLLM(_payload(
        entities=[{"name": "Caroline", "kind": "person", "facts": [0, 1]}],
        events=[{"kind": "trip", "description_fact": 0, "facts": [0],
                 "participants": [{"name": "Caroline", "role": "subject"}], "attributes": {}},
                {"kind": "trip", "description_fact": 0, "facts": [0, 1],
                 "participants": [{"name": "Bob", "role": "companion"}], "attributes": {}}]))
    report = graph_extract.extract_graph(facts, MSGS, llm=llm, retries=0)
    assert len(report.events) == 1
    assert report.events[0].facts == [0, 1]
    assert [p.name for p in report.events[0].participants] == ["Caroline", "Bob"]


# ---- 结构错误:整份重试 ----


def test_out_of_range_index_retries_then_succeeds():
    facts = [_fact("Caroline has a cat")]
    bad = _payload(entities=[{"name": "Caroline", "kind": "person", "facts": [9]}])
    good = _payload(entities=[{"name": "Caroline", "kind": "person", "facts": [0]}])
    llm = FakeLLM(bad, good)
    report = graph_extract.extract_graph(facts, MSGS, llm=llm, retries=1)
    assert report.attempts == 2 and len(report.entities) == 1
    assert "上一次输出" in llm.prompts[1][-1].content            # 纠错消息带上了原因
    assert len(report.dropped) == 0


def test_out_of_range_index_fails_after_retries_exhausted():
    facts = [_fact("Caroline has a cat")]
    bad = _payload(entities=[{"name": "Caroline", "kind": "person", "facts": [9]}])
    with pytest.raises(MemoryExtractionError, match="图提取结果无法解析"):
        graph_extract.extract_graph(facts, MSGS, llm=FakeLLM(bad, bad), retries=1)


def test_structural_violations_raise():
    facts = [_fact("Caroline has a cat")]
    cases = [
        "not json at all",
        "[1, 2, 3]",                                            # 顶层不是对象
        '{"entities": []}',                                     # 三个键必须显式给出
        _payload(entities=[{"name": "Caroline", "kind": "pet", "facts": [0]}]),   # 枚举外 kind
        _payload(entities=[{"name": "Caroline", "kind": "person", "facts": [0],
                            "extra": 1}]),                      # 额外字段
    ]
    for content in cases:
        with pytest.raises(MemoryExtractionError):
            graph_extract.extract_graph(facts, MSGS, llm=FakeLLM(content), retries=0)


def test_entity_kind_enum_matches_models():
    from app.memory.graph.models import KNOWN_ENTITY_KINDS

    assert set(graph_extract.EntityKind.__args__) == set(KNOWN_ENTITY_KINDS)


# ---- 阶段结果复用 ----


def test_dump_and_restore_roundtrip():
    facts = [_fact("Caroline has a cat named Rex")]
    llm = FakeLLM(_payload(
        entities=[{"name": "Caroline", "kind": "person", "facts": [0]},
                  {"name": "Rex", "kind": "animal", "facts": [0]}],
        relations=[{"subject": "Caroline", "relation": "has_pet", "object_entity": "Rex",
                    "object_text": "", "facts": [0]}]))
    report = graph_extract.extract_graph(facts, MSGS, llm=llm, retries=0)

    packed = graph_extract.dump(report)
    again = graph_extract.restore(packed, facts=facts)
    assert [e.name for e in again.entities] == ["Caroline", "Rex"]
    assert again.relations[0].object_entity == again.entities[1].ref

    # 复用不等于免检:编号越界、内容对不上原文,都要在 restore 里拦下(丢件或报错)
    tampered = json.loads(packed)
    tampered["entities"][0]["facts"] = [9]
    with pytest.raises(MemoryExtractionError):
        graph_extract.restore(json.dumps(tampered, ensure_ascii=False), facts=facts)

    tampered = json.loads(packed)
    tampered["entities"][0]["name"] = "Someone Else"
    still = graph_extract.restore(json.dumps(tampered, ensure_ascii=False), facts=facts)
    assert any("实体名在事实正文里找不到" in r for r in still.dropped)
