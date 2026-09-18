"""流式 /chat/stream 多事件 SSE 的离线单测:用假图喂合成流,不接 LLM。"""

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.config import get_settings
from app.main import app
from app.skills import loader

_TRAVEL = """---
id: travel
name: 差旅报销
description: 出差交通住宿费用报销
triggers:
  - 差旅
---
# 差旅报销
1. 填报销单
"""


class _Chunk:
    """messages 流里的增量,只需 content / tool_call_chunks 两个属性(鸭子类型)。"""

    def __init__(self, content="", tool_call_chunks=None):
        self.content = content
        self.tool_call_chunks = tool_call_chunks


class _FakeGraph:
    def __init__(self, items):
        self._items = items

    def astream(self, inputs, stream_mode=None, config=None):
        items = self._items

        async def gen():
            for it in items:
                yield it

        return gen()


def _fake_run():
    """模拟一次真实 ReAct:决定调 get_sop → 执行 → 逐 token 产出最终答案。"""
    tool_call = {"name": "get_sop", "args": {"skill_id": "travel"}, "id": "c1", "type": "tool_call"}
    return [
         # 第一轮 agent(step 0):先吐一段思考铺垫,再决定调工具
        ("messages", (_Chunk(content="我先查一下"), {"langgraph_node": "agent"})),
        ("updates", {"agent": {"messages": [AIMessage(content="我先查一下", tool_calls=[tool_call])]}}),
        # 工具执行:ToolMessage 也会走 messages 流(node=tools),必须被挡掉,不能灌进答案
        ("messages", (_Chunk(content="# 差旅报销 全文……"), {"langgraph_node": "tools"})),
        ("updates", {"tools": {"messages": [ToolMessage(content="# 差旅报销 ...", name="get_sop", tool_call_id="c1")]}}),
        # 边拼工具、边有零碎 content 的增量 → 因带 tool_call_chunks 被过滤
        ("messages", (_Chunk(content="稍等", tool_call_chunks=[{"index": 0}]), {"langgraph_node": "agent"})),
        # 第二轮 agent:逐 token 产出最终答案
        ("messages", (_Chunk(content="第一步:"), {"langgraph_node": "agent"})),
        ("messages", (_Chunk(content="填报销单"), {"langgraph_node": "agent"})),
        ("updates", {"agent": {"messages": [AIMessage(content="第一步:填报销单")]}}),
    ]


def _prepare(tmp_path, monkeypatch):
    (tmp_path / "travel.md").write_text(_TRAVEL, encoding="utf-8")
    monkeypatch.setenv("SOPS_DIR", str(tmp_path))
    get_settings.cache_clear()
    loader.reload()
    monkeypatch.setattr("app.api.routes.chat.get_graph", lambda: _FakeGraph(_fake_run()))


def _body(tmp_path, monkeypatch) -> str:
    _prepare(tmp_path, monkeypatch)
    prefix = get_settings().api_prefix
    client = TestClient(app)
    r = client.post(f"{prefix}/chat/stream", json={"message": "差旅怎么报销"})
    assert r.status_code == 200
    return r.text


def test_stream_emits_tool_call(tmp_path, monkeypatch):
    body = _body(tmp_path, monkeypatch)
    assert "event: tool_call" in body
    assert "get_sop" in body
    assert '"skill_id": "travel"' in body
    assert '"step": 0' in body


def test_stream_tokens_carry_step(tmp_path, monkeypatch):
    body = _body(tmp_path, monkeypatch)
    # 思考铺垫在第 0 轮
    assert '"content": "我先查一下", "step": 0' in body
    # 工具跑完后 step 前进,最终答案在第 1 轮
    assert '"content": "填报销单", "step": 1' in body


def test_stream_emits_tool_result(tmp_path, monkeypatch):
    body = _body(tmp_path, monkeypatch)
    assert "event: tool_result" in body


def test_stream_streams_only_final_answer_tokens(tmp_path, monkeypatch):
    body = _body(tmp_path, monkeypatch)
    assert "event: token" in body
    assert "第一步" in body
    assert "填报销单" in body
    # 带 tool_call_chunks 的中间增量不应作为 token 泄漏出去
    assert "稍等" not in body
    # tools 节点的工具返回文本(经 messages 流)不应灌进 token
    assert "全文" not in body


def test_stream_done_carries_skill(tmp_path, monkeypatch):
    body = _body(tmp_path, monkeypatch)
    assert "event: done" in body
    assert '"skill": "travel"' in body
