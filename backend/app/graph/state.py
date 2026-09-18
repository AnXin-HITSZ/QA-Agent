"""ReAct 对话图的共享状态。"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class ChatState(TypedDict):
    # 对话消息;add_messages 负责把节点返回的新消息(含工具调用与结果)追加进来。
    # 这既是单次提问内 ReAct 循环的 scratchpad,也是将来接 checkpointer 后的会话载体。
    messages: Annotated[list, add_messages]
