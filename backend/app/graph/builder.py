"""构建并编译 ReAct 对话图:START → agent ⇄ tools → END。

agent 节点吐出最终回答或工具调用;tools_condition 判定:
有 tool_calls → tools(执行后回到 agent),否则 → END。
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.graph.nodes import agent
from app.graph.state import ChatState
from app.graph.tools import TOOLS


def build_graph(checkpointer=None):
    g = StateGraph(ChatState)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode(TOOLS))

    g.add_edge(START, "agent")
    # agent 有 tool_calls → "tools",否则 → END(END 由 tools_condition 内部处理)。
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")

    # checkpointer 由调用方注入(main.py 的 lifespan 挂 Redis Saver → 跨轮记忆);
    # 传 None 则无持久化,仅单次提问内的 ReAct scratchpad(测试 / 未配置 Redis 时的兜底)。
    return g.compile(checkpointer=checkpointer)


@lru_cache
def get_graph():
    # 无 checkpointer 的兜底图:测试直接用,路由在未注入带记忆的图时也回退到它。
    return build_graph()
