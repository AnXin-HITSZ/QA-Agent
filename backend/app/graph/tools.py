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
from app.skills.images import referenced_images, register_images

logger = logging.getLogger(__name__)


@tool
def list_sops() -> str:
    """列出全部 SOP(标准作业流程)的元信息,供你选择适合用户问题的流程。

    何时用:用户想知道有哪些流程 / 某类事项怎么办,或你需要判断哪篇 SOP 适用。
    无需参数。返回全部 SOP 的 id、名称、简介和触发词,不包含正文、不做关键词筛选。
    根据用户问题与元信息选择合适的 id,再用 get_sop 读取完整正文后给出流程指引。
    没有合适的 SOP 时如实说明,不要编造流程。
    """
    catalog = loader.get_catalog()
    if not catalog:
        return "当前没有可用的 SOP。"
    blocks = [
        f"- id: {meta.id}\n  名称: {meta.name}\n  简介: {meta.description}\n  触发词: {', '.join(meta.triggers)}"
        for meta in catalog
    ]
    return "以下是全部可用 SOP:\n" + "\n".join(blocks)


@tool
def get_sop(skill_id: str) -> str:
    """按 id 读取某篇 SOP 的完整正文,据此给出分步指引。

    skill_id:来自 list_sops 返回的 id。
    找不到时返回提示,可改用 list_sops 重新查看目录;不要编造 SOP 中没有的内容。
    """
    found = loader.get_skill(skill_id)
    if not found:
        return f"找不到 id 为 '{skill_id}' 的 SOP。请用 list_sops 查看可用的 SOP。"
    meta, body = found
    refs = referenced_images(body)
    images = ""
    if refs:
        images = "\n\n可按需查看的图片（调用 read_sop_image）：\n" + "\n".join(
            f"- image_id: {i}; 说明: {alt}" for i, alt in refs.items()
        )
    return f"# {meta.name}(id: {meta.id})\n\n{body}{images}"


@tool(response_format="content_and_artifact")
def read_sop_image(skill_id: str, image_ids: list[str]):
    """查看 SOP 正文中的图片,用于理解操作截图、流程图、表格等。

    先调用 get_sop 读取正文及图片清单,只有需要图片信息时才调用本工具。
    skill_id 是 SOP 标识;image_ids 是该正文清单中的图片 ID,每次 1 至 4 张。
    系统会将原图作为多模态工具消息提供给你,无需另外调用 OCR 或视觉模型。
    图片加载失败时如实说明;区分截图内容与政策正文,不要凭图片猜测政策。
    """
    return register_images(skill_id, image_ids)


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
TOOLS = [list_sops, get_sop, read_sop_image, search_knowledge]
