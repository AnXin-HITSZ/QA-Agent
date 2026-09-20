"""从 ReAct 消息轨迹里回读元信息,供 API 层回填(不参与图执行)。"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, ToolMessage

from app.graph.tools import get_sop, search_knowledge
from app.skills import loader


def used_sop_id(messages: list) -> str | None:
    """倒推本次回答实际引用了哪篇 SOP,供前端「匹配条」展示。

    取消息轨迹里最后一次 ``get_sop`` 工具调用的 ``skill_id``,且该 id 必须对应
    真实存在的 SOP(过滤掉模型调用了不存在 id 的情况);若最后一次无效则回退到
    更早的有效调用。纯答疑、未读任何 SOP 时返回 None。
    """
    for msg in reversed(messages):
        for call in reversed(getattr(msg, "tool_calls", None) or []):
            if call.get("name") != get_sop.name:
                continue
            skill_id = (call.get("args") or {}).get("skill_id")
            if skill_id and loader.get_skill(str(skill_id)):
                return str(skill_id)
    return None


def used_sources(messages: list, top_n: int = 5) -> list[dict]:
    """回读本轮实际检索到的知识库来源,供前端「参考来源」展示、API 层签成短时效 URL。

    只看最后一个 HumanMessage 之后的消息(限定本轮:checkpointer 回放的是全量历史,
    不限定会把更早轮次的旧来源串进来);从 ``search_knowledge`` 工具结果的 ``artifact``
    里取命中,按 oss_key 去重(同一文件多个切块只列一次,保留分数最高者),按分数降序取
    前 top_n。未检索 / 零命中时返回空列表。
    """
    # 定位本轮起点:最后一个 HumanMessage 的位置(流式累积的 seen 里无 HumanMessage → start=0,
    # 但 seen 本就只含本轮消息,全扫也正确)。
    start = 0
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            start = i
            break
    best: dict[str, dict] = {}
    for msg in messages[start:]:
        if not isinstance(msg, ToolMessage) or getattr(msg, "name", None) != search_knowledge.name:
            continue
        for hit in getattr(msg, "artifact", None) or []:
            key = (hit or {}).get("oss_key")
            if not key:
                continue
            score = hit.get("score") or 0.0
            prev = best.get(key)
            if prev is None or score > (prev.get("score") or 0.0):
                best[key] = hit
    return sorted(best.values(), key=lambda h: h.get("score") or 0.0, reverse=True)[:top_n]
