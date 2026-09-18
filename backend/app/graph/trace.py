"""从 ReAct 消息轨迹里回读元信息,供 API 层回填(不参与图执行)。"""

from __future__ import annotations

from app.graph.tools import get_sop
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
