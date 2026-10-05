"""SOP 识图:引用校验、临时图片消息、检查点与历史回放（全部离线）。"""

import copy
import io
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from PIL import Image

from app.api.routes.conversations import _replay
from app.config import get_settings
from app.graph.builder import build_graph
from app.graph.tools import get_sop, read_sop_image
from app.graph.trace import used_images
from app.main import app
from app.skills import images, loader

A = "a" * 32 + ".png"
B = "b" * 32 + ".png"


def markdown(image_id=A):
    return f"---\nid: travel\nname: 差旅报销\n---\n正文\n![操作截图]({images.image_url(image_id)})"


@pytest.fixture
def store(monkeypatch):
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), "green").save(buf, format="PNG")

    class Store:
        def __init__(self):
            self.files = {"travel.md": markdown().encode(), f"images/{A}": buf.getvalue(), f"images/{B}": buf.getvalue()}

        def get_object(self, key):
            return self.files[key]

        def object_exists(self, key):
            return key in self.files

        def stat(self, key):
            return {"size": len(self.files[key])} if key in self.files else None

        def list_all(self):
            return [{"key": k} for k in self.files]

    result = Store()
    monkeypatch.setattr(images.oss, "sops_store", lambda: result)
    loader._load.cache_clear()
    yield result
    loader._load.cache_clear()


def read_message(image_id=A):
    return read_sop_image.invoke({
        "type": "tool_call", "id": "read1", "name": "read_sop_image",
        "args": {"skill_id": "travel", "image_ids": [image_id]},
    })


def test_markdown_references_and_code_exclusion():
    url = images.image_url(A)
    body = f"![inline]({url})\n![reference][img]\n\n[img]: {images.image_url(B)}\n"
    assert set(images.referenced_images(body)) == {A, B}
    for fake in (f"`![fake]({url})`", f"```md\n![fake]({url})\n```", f"[link]({url})", f"![external](https://other.test{url})"):
        assert images.referenced_images(fake) == {}


def test_get_sop_lists_ids_and_rejects_unreferenced_images(store):
    assert f"image_id: {A}" in get_sop.invoke({"skill_id": "travel"})
    assert not read_message(B).artifact
    assert not images.register_images("../travel", [A])[1]
    assert not images.register_images("travel", ["../secret"])[1]
    assert not images.register_images("travel", [])[1]


def test_runtime_blocks_do_not_mutate_persisted_message(store):
    message = read_message()
    before = copy.deepcopy(message.model_dump())
    serde = JsonPlusSerializer()
    restored = serde.loads_typed(serde.dumps_typed(message))
    prepared = images.prepare_image_messages([restored])
    assert any(b.get("type") == "image_url" for b in prepared[0].content)
    assert prepared[0].tool_call_id == message.tool_call_id
    assert message.model_dump() == before
    assert restored.model_dump() == before
    assert "base64" not in json.dumps(before)
    assert "signature" not in json.dumps(before)


def test_replaced_or_deleted_sop_keeps_old_image(store):
    old = read_message()
    store.files["travel.md"] = markdown(B).encode()
    assert not read_message(A).artifact  # 新工具调用按当前正文验证
    new = read_message(B)
    assert new.artifact["images"][0]["image_id"] == B
    del store.files["travel.md"]
    prepared = images.prepare_image_messages([old])
    assert any(b.get("type") == "image_url" for b in prepared[0].content)
    replay = _replay([HumanMessage(content="旧问题"), old, AIMessage(content="旧回答")])
    assert replay[-1].images[0].image_id == A
    assert replay[-1].images[0].url == images.image_url(A)


def test_missing_old_image_is_explicit_and_other_messages_survive(store):
    old = read_message()
    del store.files[f"images/{A}"]
    human = HumanMessage(content="继续")
    prepared = images.prepare_image_messages([old, human])
    assert "原件不可用" in str(prepared[0].content)
    assert all(b["type"] == "text" for b in prepared[0].content)
    assert prepared[1] is human


def test_context_limit_and_bad_artifact(store, monkeypatch):
    old = read_message()
    monkeypatch.setattr(images, "MAX_CONTEXT_BYTES", 1)
    assert "原件不可用或超限" in str(images.prepare_image_messages([old])[0].content)
    invalid = old.model_copy(deep=True)
    invalid.artifact["images"][0]["oss_key"] = "secret.txt"
    assert images.message_images(invalid) == []


def test_replay_and_current_turn_isolation(store):
    old = read_message()
    history = [HumanMessage(content="图里是什么"), old, AIMessage(content="说明"), HumanMessage(content="谢谢"), AIMessage(content="不客气")]
    assert len(_replay(history)[1].images) == 1
    assert _replay(history)[-1].images == []
    assert used_images(history) == []
    assert len(used_images([old])) == 1  # 流式 seen


@pytest.mark.asyncio
async def test_graph_checkpointer_and_resume_after_sop_change(store, monkeypatch):
    calls = []

    class Model:
        def bind_tools(self, tools):
            assert "read_sop_image" in {t.name for t in tools}
            return self

        async def ainvoke(self, messages):
            calls.append(messages)
            if len(calls) == 1:
                return AIMessage(content="", tool_calls=[{"name": "read_sop_image", "args": {"skill_id": "travel", "image_ids": [A]}, "id": "read1", "type": "tool_call"}])
            assert any(isinstance(m, ToolMessage) and isinstance(m.content, list) and any(b.get("type") == "image_url" for b in m.content) for m in messages)
            return AIMessage(content="图片回答")

    monkeypatch.setattr("app.graph.nodes.get_llm", lambda: Model())
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "image-test"}}
    result = await graph.ainvoke({"messages": [HumanMessage(content="看图")]}, config=config)
    assert isinstance(next(m for m in result["messages"] if isinstance(m, ToolMessage)).content, str)
    store.files["travel.md"] = markdown(B).encode()
    result = await graph.ainvoke({"messages": [HumanMessage(content="解释刚才图片")]}, config=config)
    assert result["messages"][-1].content == "图片回答"
    checkpoint = await graph.aget_state(config)
    assert "base64" not in str(checkpoint.values)
    assert A in str(checkpoint.values)


@pytest.fixture
def conv_dir(auth_db):
    """真会话目录(临时 SQLite)+ 一条叫 test 的历史会话:回放要求 MySQL 里有归属行。"""
    from app.auth import store as auth_store
    from tests.conftest import STUB_ADMIN_ID

    with auth_db.db.session_scope() as session:
        auth_store.create_conversation(session, conv_id="c-test", user_id=STUB_ADMIN_ID,
                                       thread_id="test", title="历史会话",
                                       now=auth_db.db.utc_naive())
    return auth_db


def test_api_stream_nonstream_and_history_return_stable_images(conv_dir, store, monkeypatch):
    tool = read_message()
    messages = [HumanMessage(content="问题"), tool, AIMessage(content="答案")]

    class Graph:
        async def ainvoke(self, *args, **kwargs):
            return {"messages": messages}

        async def astream(self, *args, **kwargs):
            yield "updates", {"tools": {"messages": [tool]}}
            yield "updates", {"agent": {"messages": [messages[-1]]}}

    class Checkpointer:
        async def aget_tuple(self, config):
            return SimpleNamespace(checkpoint={"channel_values": {"messages": messages}})

    monkeypatch.setattr(app.state, "graph", Graph(), raising=False)
    monkeypatch.setattr(app.state, "checkpointer", Checkpointer(), raising=False)
    client = TestClient(app)
    prefix = get_settings().api_prefix
    result = client.post(f"{prefix}/chat", json={"message": "问题"}).json()
    assert result["images"][0]["image_id"] == A
    stream = client.post(f"{prefix}/chat/stream", json={"message": "问题"}).text
    assert '"images": [' in stream and images.image_url(A) in stream
    assert "base64" not in stream
    history = client.get(f"{prefix}/conversations/test").json()
    assert history["messages"][-1]["images"][0]["image_id"] == A
