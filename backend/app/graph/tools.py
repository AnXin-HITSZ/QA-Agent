"""ReAct agent 可调用的工具:检索 SOP 目录、读取 SOP 全文。

工具返回给 LLM 阅读的**文本**(而非结构化对象),函数 docstring 即 LLM 看到的
用法说明——写清楚「何时用 / 参数 / 返回」直接影响模型调用质量。
"""

from __future__ import annotations

from langchain_core.tools import tool

from app.skills import loader
from app.skills.search import search_sops as _search_sops


@tool
def search_sops(query: str, top_k: int = 5) -> str:
    """搜索实验室所有 SOP(标准作业流程),按相关度返回候选清单。

    何时用:用户想知道有哪些流程 / 某类事项怎么办,或你需要判断哪篇 SOP 适用。
    query:关键词或用户问题;留空则列出全部 SOP。
    返回每条候选的 id、名称、简介与命中片段;拿到合适的 id 后再用 get_sop 读全文。
    """
    hits = _search_sops(query, top_k=top_k)
    if not hits:
        return "没有找到匹配的 SOP。可留空 query 调用一次以查看全部可用 SOP。"
    blocks = [
        f"- id: {h.id}\n  名称: {h.name}\n  简介: {h.description}\n  片段: {h.snippet}"
        for h in hits
    ]
    return "找到以下 SOP:\n" + "\n".join(blocks)


@tool
def get_sop(skill_id: str) -> str:
    """按 id 读取某篇 SOP 的完整正文,据此给出分步指引。

    skill_id:来自 search_sops 返回的 id。
    找不到时返回提示,可改用 search_sops 重新查找;不要编造 SOP 中没有的内容。
    """
    found = loader.get_skill(skill_id)
    if not found:
        return f"找不到 id 为 '{skill_id}' 的 SOP。请用 search_sops 查看可用的 SOP。"
    meta, body = found
    return f"# {meta.name}(id: {meta.id})\n\n{body}"


# 供图在 step 3 绑定到 LLM。
TOOLS = [search_sops, get_sop]
