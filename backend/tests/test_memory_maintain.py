"""维护决策层(§6):提示词、解析、授权、全有或全无、有限重试。

这一层刻意没有任何数据库 / 网络副作用,所以上面几件事可以在单元测试里直接钉死。
真实模型会不会按协议输出、判得准不准,只能在有凭证的环境里手工看 ——
用例里的「模型」是脚本化的替身,验证的是**我们**拿到输出之后做了什么。

本文件的基调:**任何一个事件不合法,整条决策都不执行**。所以「坏事件」用例断言的是
抛 MemoryDecisionRejected / MemoryExtractionError,而不是「丢掉那一条、其余照做」——
后者正是「非法维护决策被部分执行」的成因(DELETE + ADD 只做一半 = 永久丢内容)。
"""

from __future__ import annotations

import json
import logging

import pytest

from app.memory import maintain
from app.memory.errors import MemoryDecisionRejected, MemoryExtractionError
from tests.memorykit import FakeLLM

FACT = "用户把实验记录的保存格式改成了 Markdown"
ID_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
ID_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
CANDIDATES = [maintain.Candidate(memory_id=ID_A, text="用户偏好用 Word 写实验记录", revision=1),
              maintain.Candidate(memory_id=ID_B, text="用户每周五整理实验数据", revision=3)]


def _payload(*events: dict, reason: str = "测试") -> str:
    return json.dumps({"events": list(events), "reason": reason}, ensure_ascii=False)


def _decide(content: str, *, candidates=CANDIDATES, **kwargs):
    llm = FakeLLM(content)
    decision = maintain.decide(FACT, candidates, llm=llm, **kwargs)
    return decision, llm


def _rejects(content: str, *, candidates=CANDIDATES, error=MemoryDecisionRejected, **kwargs):
    """整条决策被拒:断言**一次都没有**部分执行(返回的 llm 可继续查调用次数)。"""
    llm = FakeLLM(content)
    with pytest.raises(error):
        maintain.decide(FACT, candidates, llm=llm, retries=0, **kwargs)
    return llm


# ---- 解析 ----


def test_add_event_is_kept_and_reported():
    decision, llm = _decide(_payload({"event": "ADD", "text": "用户改用 Markdown 记实验记录"},
                                     reason="全新的信息"))

    assert [type(e) for e in decision.events] == [maintain.AddEvent]
    assert decision.attempts == 1
    assert decision.reason == "全新的信息"
    assert len(llm.calls) == 1


def test_three_changes_in_one_decision_parse_into_their_own_models():
    content = _payload({"event": "UPDATE", "id": ID_A, "text": "用户偏好用 Word 或 Markdown 写实验记录"},
                       {"event": "DELETE", "id": ID_B},
                       {"event": "ADD", "text": "用户开始用 Obsidian 管理笔记"})
    decision, _ = _decide(content)

    kinds = [type(e) for e in decision.events]
    assert kinds == [maintain.UpdateEvent, maintain.DeleteEvent, maintain.AddEvent]
    assert decision.unchanged is False
    assert [(e.event, getattr(e, "id", "")) for e in decision.changes] == [
        ("UPDATE", ID_A), ("DELETE", ID_B), ("ADD", "")]


def test_none_only_means_unchanged():
    decision, _ = _decide(_payload({"event": "NONE"}))

    assert decision.unchanged is True
    assert decision.changes == []
    assert decision.dropped == []


def test_an_explicitly_empty_event_list_is_a_legal_noop():
    """`{"events": []}` 合法(明确地什么都不用做),不报错、不重试。"""
    decision, llm = _decide(json.dumps({"events": [], "reason": "没有可比的旧记忆"}))

    assert decision.events == [] and decision.unchanged is True
    assert decision.reason == "没有可比的旧记忆"
    assert len(llm.calls) == 1                    # 不是格式错误:不重试


def test_missing_events_field_is_a_format_error_not_a_noop():
    """`{}`(没有 events 字段)是**格式错误**:与 `{"events": []}` 绝不能混为一谈。

    给默认空列表会让「模型什么都没按协议输出」被静默当成「不用维护」。
    """
    llm = FakeLLM("{}", "{}")
    with pytest.raises(MemoryExtractionError):
        maintain.decide(FACT, CANDIDATES, llm=llm, retries=0)

    assert len(llm.calls) == 1


def test_markdown_fenced_json_is_accepted():
    fenced = "```json\n" + _payload({"event": "ADD", "text": "用户偏好深色主题"}) + "\n```"

    decision, _ = _decide(fenced)

    assert len(decision.changes) == 1


def test_garbage_output_retries_once_then_succeeds():
    llm = FakeLLM("抱歉,我认为不需要修改记忆。", _payload({"event": "NONE"}))

    decision = maintain.decide(FACT, CANDIDATES, llm=llm)

    assert decision.attempts == 2
    assert len(llm.calls) == 2
    # 重试时把上一次的输出与纠正说明一起带上,而不是原样再问一遍
    assert "抱歉" in llm.prompts[1] and "JSON" in llm.prompts[1]


def test_exhausted_retries_raise_instead_of_silently_doing_nothing():
    llm = FakeLLM("还是不是 JSON")

    with pytest.raises(MemoryExtractionError) as exc:
        maintain.decide(FACT, CANDIDATES, llm=llm, retries=0)

    assert len(llm.calls) == 1
    assert "共调用 1 次" in str(exc.value)


def test_business_failure_is_not_retried_on_the_paid_endpoint():
    llm = FakeLLM(error=RuntimeError("connection reset"))

    with pytest.raises(RuntimeError):
        maintain.decide(FACT, CANDIDATES, llm=llm, retries=2)

    assert len(llm.calls) == 1          # 网络错误交给任务退避,不在付费调用上打转


def test_parse_failure_log_never_carries_the_model_output(caplog):
    secret = "用户的密码是 hunter2xyz"
    llm = FakeLLM(secret)

    with caplog.at_level(logging.WARNING, logger="app.memory.maintain"):
        with pytest.raises(MemoryExtractionError):
            maintain.decide(FACT, CANDIDATES, llm=llm, retries=0)

    assert secret not in caplog.text
    assert FACT not in caplog.text


@pytest.mark.parametrize("content,why", [
    ("", "空内容"),
    ("[]", "顶层不是对象"),
    (json.dumps({"events": [], "reason": "x", "extra": 1}), "外层多了字段"),
    (json.dumps({"facts": []}), "字段名不对"),
])
def test_structural_errors_raise(content, why):
    with pytest.raises(MemoryExtractionError):
        maintain.decide(FACT, CANDIDATES, llm=FakeLLM(content), retries=0)


# ---- 全有或全无:一个坏事件拖垮整条决策 ----


def test_one_bad_event_rejects_the_whole_decision():
    """坏事件不再「丢掉这条、其余照做」:整条决策都不生效。

    否则 DELETE 旧 + ADD 新 这类成对动作只做一半,用户的记忆就永久残了。
    """
    content = _payload({"event": "CHANGE", "id": ID_A},
                       {"event": "ADD", "text": "用户偏好表格形式的周报"})

    _rejects(content, error=MemoryExtractionError)


@pytest.mark.parametrize("bad,why", [
    ({"event": "UPDATE", "text": "只有正文没有 id"}, "UPDATE 缺 id"),
    ({"event": "UPDATE", "id": ID_A}, "UPDATE 缺正文"),
    ({"event": "DELETE", "text": "DELETE 不该带正文"}, "DELETE 带正文"),
    ({"event": "ADD", "id": ID_A, "text": "ADD 不该带 id"}, "ADD 带 id"),
    ({"event": "ADD", "text": "用户偏好 X", "priority": 3}, "ADD 带未知字段"),
    ({"event": "ADD", "text": " "}, "ADD 正文为空白"),
    ({"event": "NONE", "id": ID_A}, "NONE 不该带 id"),
])
def test_wrong_shapes_reject_the_whole_decision(bad, why):
    """事件结构错误是**整条**解析失败 → 走纠正重试,重试用尽才抛。"""
    llm = FakeLLM(_payload(bad), _payload(bad))          # 重试一次仍然错

    with pytest.raises(MemoryExtractionError):
        maintain.decide(FACT, CANDIDATES, llm=llm, retries=1)

    assert len(llm.calls) == 2 and "JSON" in llm.prompts[1]


def test_the_whole_decision_is_rejected_even_when_the_good_event_comes_first():
    """合法事件排在前面也一样:解析是整份的,不存在「前面这几条先执行」。"""
    _rejects(_payload({"event": "ADD", "text": "用户偏好表格形式的周报"},
                      {"event": "CHANGE", "id": ID_A}),
             error=MemoryExtractionError)


def test_kind_and_event_time_ride_along_with_the_event():
    """kind / event_time 与提取协议同名同义,是可省略的**合法**字段(不是多字段)。"""
    decision, _ = _decide(_payload(
        {"event": "ADD", "text": "用户开始用 Obsidian 管理笔记",
         "kind": "task", "event_time": "2026-09-30"},
        {"event": "UPDATE", "id": ID_A, "text": "用户偏好用 Markdown 写实验记录"}))

    added = decision.changes[0]
    assert added.kind == "task" and added.event_time.strftime("%Y-%m-%d") == "2026-09-30"
    assert decision.changes[1].kind is None            # 省略就是 None,不猜类型


def test_short_ids_are_rejected():
    """id 太短说明模型在编(真实 id 是 UUID):宁可不改,也不整条照做。"""
    _rejects(_payload({"event": "DELETE", "id": "a1"}), error=MemoryExtractionError)


def test_text_is_clamped_to_the_configured_limit(memory_db, monkeypatch):
    monkeypatch.setattr(memory_db.settings, "memory_max_text_chars", 12)
    decision, _ = _decide(_payload({"event": "ADD", "text": "用户" + "很" * 100}))

    assert len(decision.changes[0].text) == 12


def test_a_verbose_reason_does_not_throw_away_a_good_decision():
    """说明是附带信息:模型啰嗦几句,判定好的事件照样生效(只把说明截断)。"""
    decision, _ = _decide(json.dumps(
        {"events": [{"event": "ADD", "text": "用户偏好用 Markdown 记实验记录"}],
         "reason": "原" * 800}))

    assert [type(e) for e in decision.events] == [maintain.AddEvent]
    assert len(decision.reason) == maintain.REASON_CHARS
    assert decision.attempts == 1


def test_reason_is_optional():
    assert _decide(json.dumps({"events": [{"event": "NONE"}]}))[0].reason == ""


# ---- 授权(模型只能动本次检索交给它的候选) ----


def _authorize(decision, **kwargs):
    return maintain.authorize(decision, allowed_ids={ID_A, ID_B}, **kwargs)


def test_ids_outside_the_candidates_reject_the_whole_decision():
    """模型编造 id:整条不采纳 —— 不执行 DELETE,也不执行任何「看起来合法」的同伴。"""
    _rejects(_payload({"event": "UPDATE", "id": "cccccccc-3333-4333-8333-cccccccccccc",
                       "text": "改写"},
                      {"event": "DELETE", "id": ID_A}), error=MemoryDecisionRejected)


def test_delete_old_plus_add_new_is_kept_as_a_pair():
    decision = maintain.MaintenanceDecision(events=[
        maintain.DeleteEvent(event="DELETE", id=ID_A),
        maintain.AddEvent(event="ADD", text="用户改用 Markdown 记实验记录"),
    ])

    _authorize(decision)

    assert [e.event for e in decision.events] == ["DELETE", "ADD"]
    assert decision.dropped == []


def test_same_id_twice_rejects_the_whole_decision():
    decision = maintain.MaintenanceDecision(events=[
        maintain.UpdateEvent(event="UPDATE", id=ID_A, text="第一次改写"),
        maintain.DeleteEvent(event="DELETE", id=ID_A),
    ])

    with pytest.raises(MemoryDecisionRejected) as exc:
        _authorize(decision)
    assert "同一个 id" in str(exc.value)


def test_secret_shaped_text_rejects_the_whole_decision():
    decision = maintain.MaintenanceDecision(events=[
        maintain.AddEvent(event="ADD", text="用户的 api key = sk-abcdefghijklmnopqrstuvwxyz"),
        maintain.UpdateEvent(event="UPDATE", id=ID_A, text="密码是 hunter2xyz"),
    ])

    with pytest.raises(MemoryDecisionRejected) as exc:
        _authorize(decision)
    assert "秘密" in str(exc.value)


def test_blank_text_rejects_the_whole_decision():
    """解析路径造不出空正文(事件模型自己就拦了,见上一条);这是给直接调用方的兜底。"""
    decision = maintain.MaintenanceDecision(events=[
        maintain.AddEvent.model_construct(event="ADD", text=" 　 "),
    ])

    with pytest.raises(MemoryDecisionRejected) as exc:
        _authorize(decision)
    assert "正文为空" in str(exc.value)


def test_over_the_event_cap_rejects_the_whole_decision():
    """超限不切前 5 个:一次越权输出不能被当成部分授权。"""
    decision = maintain.MaintenanceDecision(events=[
        maintain.AddEvent(event="ADD", text=f"用户偏好方案{i}") for i in range(4)
    ])

    with pytest.raises(MemoryDecisionRejected) as exc:
        _authorize(decision, max_events=2)
    assert "上限" in str(exc.value)


def test_max_events_default_comes_from_settings(memory_db, monkeypatch):
    monkeypatch.setattr(memory_db.settings, "memory_maintenance_max_actions", 1)
    decision = maintain.MaintenanceDecision(events=[
        maintain.AddEvent(event="ADD", text="用户偏好 A"),
        maintain.AddEvent(event="ADD", text="用户偏好 B"),
    ])

    with pytest.raises(MemoryDecisionRejected):
        maintain.authorize(decision, allowed_ids=set())


@pytest.mark.parametrize("events", [
    ({"event": "NONE"}, {"event": "ADD", "text": "用户偏好 A"}),      # NONE 在前
    ({"event": "ADD", "text": "用户偏好 A"}, {"event": "NONE"}),      # NONE 在后
])
def test_none_mixed_with_real_changes_is_contradictory(events):
    """NONE = 「不用动」(全流程同一个含义),与要执行的改动同时出现是自相矛盾。"""
    _rejects(_payload(*events), error=MemoryDecisionRejected)


def test_repeated_none_is_normalized_not_dropped():
    """合法的重复 NONE 合并成一条,并说明 —— 那是规范化,不是丢事件。"""
    decision, _ = _decide(_payload({"event": "NONE"}, {"event": "NONE"}))

    assert len(decision.events) == 1 and decision.unchanged is True
    assert any("NONE" in d for d in decision.dropped)


def test_rejection_retries_with_a_corrective_prompt_then_raises():
    """越权 / 自相矛盾是模型能自己改对的一类错误:带纠正提示重试,重试用尽才明确失败。"""
    bad = _payload({"event": "DELETE", "id": ID_B}, {"event": "UPDATE", "id": ID_B, "text": "重复引用"})
    good = _payload({"event": "DELETE", "id": ID_B})
    llm = FakeLLM(bad, good)

    decision = maintain.decide(FACT, CANDIDATES, llm=llm, retries=1)

    assert [e.event for e in decision.events] == ["DELETE"]
    assert decision.attempts == 2 and len(llm.calls) == 2
    assert "JSON" in llm.prompts[1]                 # 第二次带上了纠正说明


def test_retries_default_comes_from_settings(memory_db, monkeypatch):
    monkeypatch.setattr(memory_db.settings, "memory_maintenance_retries", 0)
    llm = FakeLLM("不是 JSON")

    with pytest.raises(MemoryExtractionError):
        maintain.decide(FACT, CANDIDATES, llm=llm)

    assert len(llm.calls) == 1


def test_authorization_only_covers_the_candidates_actually_shown():
    """授权集合 = **真正展示给模型**的那批候选(被预算裁掉的它根本没见过)。

    「引用只能来自展示过的候选」这条保证落在 visible_candidates 上,不能两处各算一遍。
    """
    filler = "填充" * (maintain.CANDIDATE_CHARS // 2 + 10)
    many = [maintain.Candidate(f"{i:04d}-cand", filler, revision=1) for i in range(60)]
    shown = {c.memory_id for c in maintain.visible_candidates(many)}

    assert "0000-cand" in shown and "0059-cand" not in shown
    decision = maintain.MaintenanceDecision(events=[
        maintain.DeleteEvent(event="DELETE", id="0059-cand"),
    ])
    with pytest.raises(MemoryDecisionRejected):
        maintain.authorize(decision, allowed_ids=shown)


# ---- 决策复用(存下来的决策重新过一遍同一套协议) ----


def test_a_stored_decision_round_trips_through_restore():
    decision = maintain.MaintenanceDecision(events=[
        maintain.DeleteEvent(event="DELETE", id=ID_A),
        maintain.AddEvent(event="ADD", text="用户改用 Markdown 记实验记录", kind="preference"),
    ], reason="旧说法被推翻")

    restored = maintain.restore(maintain.dump(decision), allowed_ids={ID_A, ID_B})

    assert [e.event for e in restored.events] == ["DELETE", "ADD"]
    assert restored.reason == "旧说法被推翻"
    assert restored.changes[1].kind == "preference"


def test_restore_still_enforces_authorization_and_shape():
    """复用不等于免检:存下来的决策同样要过 id 授权与结构校验。"""
    with pytest.raises(MemoryDecisionRejected):
        maintain.restore(_payload({"event": "DELETE", "id": ID_B}), allowed_ids={ID_A})
    with pytest.raises(MemoryExtractionError):
        maintain.restore("{}", allowed_ids={ID_A})


# ---- 提示词 ----


def _human(decision_messages) -> str:
    return decision_messages[1].content


def test_prompt_carries_the_fact_and_the_candidates():
    messages = maintain.build_messages(FACT, CANDIDATES)

    system, human = messages[0].content, _human(messages)
    assert "ADD" in system and "UPDATE" in system and "DELETE" in system and "NONE" in system
    assert "只输出 JSON" in system
    assert FACT in human
    for cand in CANDIDATES:
        assert cand.memory_id in human and cand.text in human


def test_prompt_says_an_illegal_event_voids_the_whole_decision():
    """提示词要把后果讲清楚:模型知道「拿不准就 NONE」比事后再补要便宜。"""
    system = maintain.build_messages(FACT, CANDIDATES)[0].content

    assert "整份决策都会被拒绝" in system


def test_prompt_without_candidates_says_so():
    assert "(无)" in _human(maintain.build_messages(FACT, []))


def test_long_candidate_is_truncated_in_the_prompt():
    long_text = "用户" + "很" * (maintain.CANDIDATE_CHARS * 2)
    messages = maintain.build_messages(FACT, [maintain.Candidate(ID_A, long_text, 1)])

    human = _human(messages)
    assert len(human) < maintain.CANDIDATE_CHARS * 2
    assert "很" * maintain.CANDIDATE_CHARS not in human


def test_candidate_budget_stops_adding_more_candidates():
    filler = "填充" * (maintain.CANDIDATE_CHARS // 2 + 10)
    many = [maintain.Candidate(f"{i:04d}-cand", filler, revision=1) for i in range(60)]

    human = _human(maintain.build_messages(FACT, many))

    assert "0000-cand" in human and "0059-cand" not in human
    assert len(human) < maintain.CONTEXT_LIMIT + 500      # 预算之外只剩模板与事实本身
