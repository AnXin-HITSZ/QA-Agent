"""ReAct 图装配单测:验证能编译、节点与边齐全(不触发 LLM,离线可跑)。

真实推理 / 工具调用需真实模型,交由带真 key 的端到端冒烟验证。
"""

from app.graph import get_graph


def test_react_graph_compiles_with_expected_nodes():
    compiled = get_graph()
    drawable = compiled.get_graph()
    names = set(drawable.nodes.keys())
    assert "agent" in names
    assert "tools" in names


def test_agent_loops_back_from_tools():
    compiled = get_graph()
    drawable = compiled.get_graph()
    # tools 执行完必须回到 agent(ReAct 循环),而非直接结束。
    edges = {(e.source, e.target) for e in drawable.edges}
    assert ("tools", "agent") in edges
