"""历史对话检索单测:纯函数(切词 / 命中 / 片段)+ 端点级(假 checkpointer,不接 Redis)。"""

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from app.api.routes.conversations import _match_of, _snippet, _terms
from app.config import get_settings
from app.main import app
from app.schemas.conversation import ConversationMessage


def _msg(role: str, content: str) -> ConversationMessage:
    return ConversationMessage(role=role, content=content)


def test_terms_splits_on_whitespace():
    assert _terms("  发票   报销 ") == ["发票", "报销"]
    assert _terms("") == []


def test_match_requires_all_terms_in_one_message():
    replay = [_msg("user", "发票丢了怎么办"), _msg("assistant", "发票丢了要联系报销经办人")]
    # 单词命中:两条都有「发票」,片段取第一条,计数 2
    m = _match_of(replay, ["发票"])
    assert m is not None
    assert (m.role, m.count) == ("user", 2)
    # 关键词分处两条消息 → 不算命中(跨消息不算,否则片段解释不了为什么命中)
    assert _match_of(replay, ["怎么办", "经办人"]) is None
    # 同处一条 → 命中,角色跟着那条消息
    m2 = _match_of(replay, ["发票", "经办人"])
    assert m2 is not None
    assert (m2.role, m2.count) == ("assistant", 1)


def test_match_is_case_insensitive():
    replay = [_msg("assistant", "见 SOP 附录 B")]
    assert _match_of(replay, ["sop"]) is not None


def test_snippet_centers_on_term_with_ellipsis():
    text = "前" * 60 + "发票" + "后" * 60
    s = _snippet(text, ["发票"])
    assert s.startswith("…") and s.endswith("…")
    assert "发票" in s
    assert len(s) <= 34 + 2 + 34 + 2  # 前后各留 34 字 + 两个省略号


def test_snippet_short_text_has_no_ellipsis():
    assert _snippet("发票", ["发票"]) == "发票"


def test_snippet_flattens_newlines():
    s = _snippet("第一行\n发票在这里\n第三行", ["发票"])
    assert "\n" not in s
    assert "发票在这里" in s


def test_snippet_uses_earliest_term():
    # 「报销」出现在更靠前的位置,片段应以它为中心
    text = "报销流程" + "中间" * 40 + "发票"
    s = _snippet(text, ["发票", "报销"])
    assert s.startswith("报销流程")


# ── 端点级:假 checkpointer 顶替 Redis,验证 ?q= 的过滤与 match 契约 ──


class _FakeTuple:
    """够用的 CheckpointTuple —— 端点只读 config / checkpoint 两处。"""

    def __init__(self, tid: str, messages: list, ts: str) -> None:
        self.config = {"configurable": {"thread_id": tid}}
        self.checkpoint = {"channel_values": {"messages": messages}, "ts": ts}


class _FakeCP:
    """假 checkpointer:alist(None) 按给定顺序产出(端点按「首见即最新」取线程)。"""

    def __init__(self, tups: list[_FakeTuple]) -> None:
        self._tups = tups

    def alist(self, config=None):  # noqa: ARG002 —— 端点固定传 None
        async def gen():
            for t in self._tups:
                yield t

        return gen()


def _tups() -> list[_FakeTuple]:
    return [
        _FakeTuple(
            "t1",
            [HumanMessage(content="发票丢了怎么办"), AIMessage(content="先登报挂失,再找经办人补办")],
            "2026-10-01T10:00:00+00:00",
        ),
        _FakeTuple("t2", [HumanMessage(content="打印机怎么连网络")], "2026-10-01T09:00:00+00:00"),
    ]


def _get(monkeypatch, query: str = "") -> dict:
    monkeypatch.setattr(app.state, "checkpointer", _FakeCP(_tups()), raising=False)
    url = f"{get_settings().api_prefix}/conversations{query}"
    r = TestClient(app).get(url)
    assert r.status_code == 200
    return r.json()


def test_list_without_q_returns_all(monkeypatch):
    data = _get(monkeypatch)
    assert [i["thread_id"] for i in data["items"]] == ["t1", "t2"]
    assert data["items"][0]["match"] is None  # 未搜索不带命中说明


def test_list_with_q_keeps_only_hits(monkeypatch):
    data = _get(monkeypatch, "?q=发票")
    assert [i["thread_id"] for i in data["items"]] == ["t1"]
    m = data["items"][0]["match"]
    assert m["role"] == "user"  # 提问先命中
    assert m["count"] == 1
    assert "发票" in m["snippet"]


def test_list_with_multi_term_q_is_and(monkeypatch):
    # 「发票」在 t1、「打印机」在 t2 → 跨对话不成立
    assert _get(monkeypatch, "?q=发票 打印机")["items"] == []
    # 两词同处 t1 的回答里 → 命中,角色为 assistant
    m = _get(monkeypatch, "?q=挂失 经办人")["items"][0]["match"]
    assert m["role"] == "assistant"


def test_list_with_q_no_hit_returns_empty(monkeypatch):
    assert _get(monkeypatch, "?q=完全不相干的词")["items"] == []
