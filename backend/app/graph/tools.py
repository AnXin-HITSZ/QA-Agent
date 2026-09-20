"""ReAct agent 可调用的工具:检索 SOP 目录、读取 SOP 全文。

工具返回给 LLM 阅读的**文本**(而非结构化对象),函数 docstring 即 LLM 看到的
用法说明——写清楚「何时用 / 参数 / 返回」直接影响模型调用质量。
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from app.rag.retrieve import format_hits
from app.rag.retrieve import search_knowledge as _search_knowledge
from app.skills import loader
from app.skills.search import search_sops as _search_sops

logger = logging.getLogger(__name__)


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


@tool(response_format="content_and_artifact")
def search_knowledge(query: str, top_k: int = 5):
    """检索实验室知识库,返回与问题最相关的资料片段(带来源),供你据此作答。

    何时用:用户的问题需要实验室内部资料 / 文档 / 制度 / 数据支撑,而你并不确定答案时,
    先用它查库,再依据返回的片段回答;检索不到再据常识回答并说明「知识库中未找到」。
    query:用户问题或检索关键词。
    返回按相关度排序的资料片段(含来源文件名);引用来源(可点链接)由系统自动附到回答里,
    你无需在正文里罗列 URL。
    """
    try:
        hits = _search_knowledge(query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 — 工具边界:任何检索失败都降级,别让异常穿透 ToolNode 打崩整轮对话
        # 覆盖:Embeddings/Qdrant 未配置或不可达(RuntimeError),以及 embedding/向量
        # 供应商返回的调用错误(如 DashScope 账户欠费的 openai.BadRequestError 400)。
        # 详细异常写服务端日志便于排查;只回给 LLM 一句干净提示,不把冗长报错喂给模型。
        logger.warning("search_knowledge 检索失败,降级返回: %s", exc)
        return ("知识库当前不可用,无法检索;请据常识回答,并说明「知识库暂时无法访问」。", [])
    if not hits:
        return ("知识库中没有找到与该问题相关的资料。", [])
    text = "找到以下相关资料:\n\n" + format_hits(hits)
    return (text, hits)


# 供图绑定到 LLM(builder.ToolNode 执行 + nodes.bind_tools 绑定,均从此导入)。
TOOLS = [search_sops, get_sop, search_knowledge]
