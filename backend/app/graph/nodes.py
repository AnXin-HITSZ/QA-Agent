"""ReAct agent 节点:LLM(绑定工具)推理 → 需要时发起工具调用 → 依据结果作答。

工具经 bind_tools 通过模型原生 tool-calling 通道注入,不写进 prompt;
system prompt 只立行为护栏(何时检索、严格依据 SOP、不编造),不点名具体工具。
"""

from __future__ import annotations

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig

from app.graph.state import ChatState
from app.graph.tools import TOOLS
from app.llm import get_llm

SYSTEM_PROMPT = (
    "你是实验室助手。遇到报销 / 流程类问题,先检索相关 SOP、再依据其正文给出分步指引,"
    "严格按正文,可裁剪、重排以贴合提问,但不得编造其中没有的步骤或政策;"
    "检索不到就如实说明,不要臆测。"
    "日常答疑遇到需要实验室内部资料 / 制度 / 数据支撑的问题,先检索知识库、依据检索到的资料作答,"
    "查不到再据常识回答并说明「知识库中未找到」;无需资料即可回答的常识问题可直接回答。"
    "你不接入任何报销系统,只提供流程指引。"
)


async def agent(state: ChatState, config: RunnableConfig) -> dict:
    """一步推理:产出最终回答,或产出对工具的调用请求(交给 tools 节点执行)。

    待办感知(只读):chat 路由在调图前把「未完成待办」渲染好放进
    config.configurable.todos_prompt,这里追加到系统提示后 —— 不写进 state、不入
    checkpoint,故每轮都是最新;无待办 / 未启用时为空串,系统提示零变化。
    """
    model = get_llm().bind_tools(TOOLS)
    todos_prompt = (config.get("configurable") or {}).get("todos_prompt") or ""
    system = SYSTEM_PROMPT + todos_prompt
    messages = [SystemMessage(content=system), *state["messages"]]
    ai = await model.ainvoke(messages)
    return {"messages": [ai]}
