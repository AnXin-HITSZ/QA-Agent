"""历史对话单测:纯函数(切词 / 命中 / 片段)+ 端点级(真目录表 + 假 checkpointer)。

端点级用 auth_db(临时 SQLite,与迁移等价的表)放会话目录,身份沿用 autouse 的
「已登录管理员」桩 —— 归属看的**不是**怎么登录的,而是目录行里的 user_id;
正文用一个按 thread_id 取 / 删的假 checkpointer,不接 Redis。
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
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


# ── 端点级 ──
#
# 语义变了(见 routes/conversations.py 的模块说明):目录与归属在 MySQL,Redis 只存正文。
# 所以这里要真表(auth_db:临时 SQLite + 与迁移等价的表),身份继续用 autouse 的
# 「已登录管理员」桩 —— 归属看的是目录行里的 user_id,不是登录方式。
# 正文换成一个只认 thread_id 的假 checkpointer,不接 Redis。


class _FakeTuple:
    """够用的 CheckpointTuple —— 端点只读 checkpoint 里的 messages。"""

    def __init__(self, tid: str, messages: list, ts: str) -> None:
        self.config = {"configurable": {"thread_id": tid}}
        self.checkpoint = {"channel_values": {"messages": messages}, "ts": ts}


class _FakeCP:
    """假 checkpointer:按 thread_id 取 / 删,不接 Redis。

    fail_read / fail_delete 抛的是**真** RedisError(redis.exceptions.ConnectionError),
    让路由的降级分支(degraded / 503)走真判断函数,而不是把判断函数也打桩掉。
    """

    def __init__(self, threads: dict[str, list]) -> None:
        self.threads = dict(threads)
        self.deleted: list[str] = []
        self.fail_read: BaseException | None = None
        self.fail_delete = False

    @staticmethod
    def _redis_down(msg: str) -> BaseException:
        from redis.exceptions import ConnectionError as RedisConnError

        return RedisConnError(msg)

    async def aget_tuple(self, config):
        if self.fail_read is not None:
            raise self.fail_read
        tid = config["configurable"]["thread_id"]
        messages = self.threads.get(tid)
        if messages is None:
            return None
        return _FakeTuple(tid, messages, "2026-10-01T10:00:00+00:00")

    async def adelete_thread(self, tid: str) -> None:
        if self.fail_delete:
            raise self._redis_down("redis down")
        self.deleted.append(tid)
        self.threads.pop(tid, None)


OTHER_USER_ID = "00000000-0000-4000-8000-0000000000ee"


def _seed(auth_db, *, user_id: str, conv_id: str, title: str,
          minutes_ago: int = 0) -> str:
    """在目录里插一行,返回线程 id(与路由同一条 thread_id_for 拼法)。"""
    from app.auth import store
    from app.conversations import store as conv_store

    thread_id = conv_store.thread_id_for(user_id=user_id, conv_id=conv_id)
    now = auth_db.db.utc_naive() - timedelta(minutes=minutes_ago)
    with auth_db.db.session_scope() as session:
        store.create_conversation(session, conv_id=conv_id, user_id=user_id,
                                  thread_id=thread_id, title=title, now=now)
    return thread_id


MSG_1 = [HumanMessage(content="发票丢了怎么办"), AIMessage(content="先登报挂失,再找经办人补办")]
MSG_2 = [HumanMessage(content="打印机怎么连网络")]


@pytest.fixture
def conv_env(auth_db, monkeypatch):
    """本人的两条会话 + 别人的一条;别人的正文里也有「发票」,用来验证检索不越界。"""
    from tests.conftest import STUB_ADMIN_ID

    t1 = _seed(auth_db, user_id=STUB_ADMIN_ID, conv_id="c1", title="发票丢了怎么办", minutes_ago=0)
    t2 = _seed(auth_db, user_id=STUB_ADMIN_ID, conv_id="c2", title="打印机怎么连网络", minutes_ago=5)
    t_other = _seed(auth_db, user_id=OTHER_USER_ID, conv_id="c3", title="别人的会话", minutes_ago=1)
    cp = _FakeCP({
        t1: MSG_1,
        t2: MSG_2,
        t_other: [HumanMessage(content="别人的发票问题")],
    })
    monkeypatch.setattr(app.state, "checkpointer", cp, raising=False)
    return SimpleNamespace(db=auth_db, cp=cp, mine={"t1": t1, "t2": t2}, other=t_other)


def _client() -> TestClient:
    return TestClient(app)


def _url(path: str) -> str:
    return f"{get_settings().api_prefix}{path}"


# ---- 列表:只列自己的 + 真分页 ----


def test_list_returns_only_my_conversations(conv_env):
    r = _client().get(_url("/conversations"))
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is True and data["degraded"] is False
    assert [i["thread_id"] for i in data["items"]] == [conv_env.mine["t1"], conv_env.mine["t2"]]
    assert data["total"] == 2 and data["limit"] == 30
    assert data["items"][0]["match"] is None          # 未搜索不带命中说明
    assert data["items"][0]["message_count"] == 2     # 正文来自假 checkpointer
    assert conv_env.other not in [i["thread_id"] for i in data["items"]]


def test_list_paginates_in_sql(conv_env):
    page = _client().get(_url("/conversations"), params={"offset": 1, "limit": 1}).json()
    assert [i["thread_id"] for i in page["items"]] == [conv_env.mine["t2"]]
    assert (page["total"], page["offset"], page["limit"]) == (2, 1, 1)


def test_list_still_lists_conversations_without_bodies(conv_env):
    """正文缺失(线程空 / 刚建好就被中断)不该让会话从列表消失,只是没有消息数。"""
    conv_env.cp.threads.clear()                       # aget_tuple 返回 None
    data = _client().get(_url("/conversations")).json()
    assert [i["thread_id"] for i in data["items"]] == [conv_env.mine["t1"], conv_env.mine["t2"]]
    assert data["degraded"] is False                  # 读到「空」不是读失败
    assert all(i["message_count"] == 0 for i in data["items"])


def test_list_is_degraded_when_redis_errors(conv_env):
    """Redis 报错时列表照常给(目录在 MySQL),但如实标 degraded。"""
    conv_env.cp.fail_read = _FakeCP._redis_down("connection refused")
    data = _client().get(_url("/conversations")).json()
    assert [i["thread_id"] for i in data["items"]] == [conv_env.mine["t1"], conv_env.mine["t2"]]
    assert data["degraded"] is True
    assert all(i["message_count"] == 0 for i in data["items"])


# ---- 读取:归属不对 / 不存在都是 404 ----


def test_detail_returns_replayed_messages(conv_env):
    r = _client().get(_url(f"/conversations/{conv_env.mine['t1']}"))
    assert r.status_code == 200
    body = r.json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert "登报挂失" in body["messages"][1]["content"]


def test_detail_without_body_is_empty_not_404(conv_env):
    """目录里有、正文还没落盘(刚建好就被中断):200 + 空消息,而不是 404。"""
    conv_env.cp.threads.pop(conv_env.mine["t2"])
    r = _client().get(_url(f"/conversations/{conv_env.mine['t2']}"))
    assert r.status_code == 200 and r.json()["messages"] == []


def test_detail_is_503_when_redis_errors(conv_env):
    conv_env.cp.fail_read = _FakeCP._redis_down("timeout")
    r = _client().get(_url(f"/conversations/{conv_env.mine['t1']}"))
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "memory_unavailable"


def test_other_users_conversation_is_404(conv_env):
    c = _client()
    assert c.get(_url(f"/conversations/{conv_env.other}")).status_code == 404
    assert c.delete(_url(f"/conversations/{conv_env.other}")).status_code == 404
    # 而且对方那条**没被动过**:正文还在,也没进过删除流程
    assert conv_env.other in conv_env.cp.threads
    assert conv_env.cp.deleted == []


def test_unknown_thread_is_404_and_never_creates_a_row(conv_env):
    from app.conversations import store as conv_store

    fake = conv_store.thread_id_for(user_id=OTHER_USER_ID, conv_id="nope")
    c = _client()
    assert c.get(_url(f"/conversations/{fake}")).status_code == 404
    assert c.delete(_url(f"/conversations/{fake}")).status_code == 404
    assert c.get(_url("/conversations")).json()["total"] == 2   # 不存在的 id 不"顺手建一条"


# ---- 删除:状态机 active → deleting → deleted ----


def test_delete_marks_tombstone_and_wipes_bodies(conv_env):
    c = _client()
    tid = conv_env.mine["t1"]
    assert c.delete(_url(f"/conversations/{tid}")).status_code == 204
    assert conv_env.cp.deleted == [tid]                         # 正文被抹掉
    assert c.get(_url(f"/conversations/{tid}")).status_code == 404
    assert c.get(_url("/conversations")).json()["total"] == 1

    from app.auth import store

    with conv_env.db.db.session_scope() as session:
        row = store.get_conversation_by_thread_id(session, tid)
        assert row is not None
        assert row.status == store.CONV_DELETED and row.deleted_at is not None


def test_delete_waits_for_inflight_generation_before_wiping(conv_env, monkeypatch):
    """删除先等这通对话在跑的生成收尾,再抹正文(**顺序**不能反)。

    这里钉的是「路由确实先等、再删」;「等不到就超时续删」的真实语义在下面的
    inflight 用例里 —— 那些跑在同一条事件循环上(TestClient 每个请求一条新循环,
    跨循环等 Event 不可靠)。
    """
    import app.api.routes.conversations as conv_routes

    tid = conv_env.mine["t1"]
    order: list[str] = []

    async def fake_wait_idle(thread_id, timeout=None):
        order.append(f"wait:{thread_id}")
        return True

    async def recording_delete(thread_id):
        order.append(f"delete:{thread_id}")
        conv_env.cp.threads.pop(thread_id, None)

    monkeypatch.setattr(conv_routes.inflight, "wait_idle", fake_wait_idle)
    monkeypatch.setattr(conv_env.cp, "adelete_thread", recording_delete)

    assert _client().delete(_url(f"/conversations/{tid}")).status_code == 204
    assert order == [f"wait:{tid}", f"delete:{tid}"]


def test_delete_keeps_row_deleting_when_redis_is_down(conv_env):
    """Redis 删不掉 → 503,**不谎报成功**;行留在 deleting,等启动对账补删。"""
    c = _client()
    tid = conv_env.mine["t1"]
    conv_env.cp.fail_delete = True
    r = c.delete(_url(f"/conversations/{tid}"))
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "memory_unavailable"

    from app.auth import store

    with conv_env.db.db.session_scope() as session:
        row = store.get_conversation_by_thread_id(session, tid)
        assert row.status == store.CONV_DELETING and row.deleted_at is None
    # 这段期间它对用户已经不可见:不列表、也 404 —— 正是 deleting 的语义
    assert c.get(_url(f"/conversations/{tid}")).status_code == 404
    assert tid not in [i["thread_id"] for i in c.get(_url("/conversations")).json()["items"]]


def test_reconcile_finishes_a_conversation_stuck_in_deleting(conv_env):
    """上次删除卡在 deleting(接口已如实回 503):启动对账把正文与目录都删干净。"""
    import asyncio

    from app.auth import store
    from app.conversations import reconcile_deleting

    c = _client()
    tid = conv_env.mine["t1"]
    conv_env.cp.fail_delete = True
    assert c.delete(_url(f"/conversations/{tid}")).status_code == 503

    conv_env.cp.fail_delete = False
    done = asyncio.run(reconcile_deleting(conv_env.cp, older_than_seconds=0))
    assert done == 1 and tid in conv_env.cp.deleted
    with conv_env.db.db.session_scope() as session:
        row = store.get_conversation_by_thread_id(session, tid)
        assert row.status == store.CONV_DELETED and row.deleted_at is not None


# ---- 检索:只在自己**有界**的会话里搜 ----


def test_search_keeps_only_hits(conv_env):
    data = _client().get(_url("/conversations"), params={"q": "发票"}).json()
    assert [i["thread_id"] for i in data["items"]] == [conv_env.mine["t1"]]
    m = data["items"][0]["match"]
    assert m["role"] == "user" and m["count"] == 1 and "发票" in m["snippet"]


def test_search_never_leaves_my_conversations(conv_env):
    """「别人的」只出现在别人的会话里 —— 检索若越界就会命中。"""
    data = _client().get(_url("/conversations"), params={"q": "别人的"}).json()
    assert data["items"] == [] and data["total"] == 0


def test_search_multi_term_is_and(conv_env):
    c = _client()
    assert c.get(_url("/conversations"), params={"q": "发票 打印机"}).json()["items"] == []
    m = c.get(_url("/conversations"), params={"q": "挂失 经办人"}).json()["items"][0]["match"]
    assert m["role"] == "assistant"


def test_search_no_hit_returns_empty(conv_env):
    assert _client().get(_url("/conversations"),
                         params={"q": "完全不相干的词"}).json()["items"] == []


def test_search_pages_within_the_hits(conv_env):
    data = _client().get(_url("/conversations"),
                         params={"q": "发票", "offset": 1, "limit": 1}).json()
    assert data["total"] == 1 and data["items"] == []


# ---- 线程 id 与归属:字符串只用来寻址,授权一律回 MySQL ----


def test_thread_id_roundtrip():
    from app.conversations import store as conv_store

    tid = conv_store.thread_id_for(user_id=OTHER_USER_ID, conv_id="c9")
    assert conv_store.parse_thread_id(tid) == (OTHER_USER_ID, "c9")


def test_parse_thread_id_rejects_foreign_env_version_and_garbage():
    from app.conversations import store as conv_store

    env = get_settings().app_env
    good = conv_store.thread_id_for(user_id=OTHER_USER_ID, conv_id="c9")
    assert conv_store.parse_thread_id(good.replace(f":{env}:chat:", ":somewhere-else:chat:")) is None
    assert conv_store.parse_thread_id(good.replace(":chat:v1:", ":chat:v2:")) is None
    assert conv_store.parse_thread_id("") is None
    assert conv_store.parse_thread_id(f"qa:{env}:chat:v1:u:{OTHER_USER_ID}") is None   # 段数不够
    assert conv_store.parse_thread_id(f"qa:{env}:chat:v1:u:{OTHER_USER_ID}:c:") is None  # 空会话 id


class _NoGraph:
    """不该走到生成那一步:一旦被调用就大声失败。"""

    async def ainvoke(self, *args, **kwargs):
        raise AssertionError("归属不对就不该开始生成")


def test_chat_refuses_a_thread_that_is_not_mine(conv_env, monkeypatch):
    """带着别人的 thread_id 提问:404,**不**新建会话,**不**碰别人的正文。"""
    from tests.conftest import STUB_ADMIN_ID

    monkeypatch.setattr(app.state, "graph", _NoGraph(), raising=False)
    monkeypatch.setattr(app.state, "todos", None, raising=False)
    c = _client()

    r = c.post(_url("/chat"), json={"message": "把别人的会话接着聊", "thread_id": conv_env.other})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "conversation_not_found"
    assert conv_env.other in conv_env.cp.threads          # 别人的正文纹丝没动
    assert c.get(_url("/conversations")).json()["total"] == 2   # 也没有顺手建一条

    # 拼一个像别人线程、但数据库里不存在的 id:同样 404(归属看行,不看字符串)
    from app.conversations import store as conv_store

    ghost = conv_store.thread_id_for(user_id=STUB_ADMIN_ID, conv_id="ghost")
    assert c.post(_url("/chat"), json={"message": "hi", "thread_id": ghost}).status_code == 404


def test_chat_without_thread_id_creates_a_conversation_of_my_own(conv_env, monkeypatch):
    """不带 thread_id:后端建目录行并拼线程 id(客户端指定的 id 一律不认)。"""
    from tests.conftest import STUB_ADMIN_ID

    class G:
        async def ainvoke(self, inputs, config):
            self.thread_id = config["configurable"]["thread_id"]
            return {"messages": [*inputs["messages"], AIMessage(content="答")]}

    graph = G()
    monkeypatch.setattr(app.state, "graph", graph, raising=False)
    monkeypatch.setattr(app.state, "todos", None, raising=False)

    r = _client().post(_url("/chat"), json={"message": "  发票丢了怎么办  "})
    assert r.status_code == 200
    new_tid = r.json()["thread_id"]
    assert new_tid == graph.thread_id                     # 生成用的就是这条线程
    assert new_tid not in (conv_env.mine["t1"], conv_env.mine["t2"], conv_env.other)

    from app.conversations import store as conv_store

    assert conv_store.parse_thread_id(new_tid) == (STUB_ADMIN_ID, new_tid.split(":")[-1])
    # 目录里真的多了一条,标题是首条提问(去掉两端空白)
    data = _client().get(_url("/conversations")).json()
    assert data["total"] == 3
    assert next(i for i in data["items"] if i["thread_id"] == new_tid)["title"] == "发票丢了怎么办"


# ---- inflight:删除等生成收尾的**真**语义(单测跑在同一条事件循环上) ----


def test_inflight_wait_idle_returns_when_generation_finishes():
    import asyncio

    from app.conversations import inflight

    async def main():
        inflight.reset()
        released = asyncio.Event()

        async def generation():
            async with inflight.track("t-x"):
                await released.wait()

        task = asyncio.create_task(generation())
        await asyncio.sleep(0)
        assert inflight.active("t-x") == 1

        waiter = asyncio.create_task(inflight.wait_idle("t-x", timeout=2))
        await asyncio.sleep(0.01)
        assert not waiter.done()                 # 还在生成 → 得等
        released.set()
        assert await waiter is True
        await task
        assert inflight.active("t-x") == 0
        inflight.reset()

    asyncio.run(main())


def test_inflight_counts_two_generations_on_one_thread():
    """同一条线程上两轮生成:要等**两个**都跑完才放行,不是先结束的那个说了算。"""
    import asyncio

    from app.conversations import inflight

    async def main():
        inflight.reset()
        order: list[str] = []

        async def gen(name: str, delay: float):
            async with inflight.track("t-w"):
                await asyncio.sleep(delay)
                order.append(name)

        a = asyncio.create_task(gen("g1", 0.05))
        b = asyncio.create_task(gen("g2", 0.06))
        await asyncio.sleep(0.02)
        assert inflight.active("t-w") == 2
        assert await inflight.wait_idle("t-w", timeout=2) is True
        await asyncio.gather(a, b)
        assert order == ["g1", "g2"]             # 两个都结束了才返回
        assert inflight.active("t-w") == 0
        inflight.reset()

    asyncio.run(main())


def test_inflight_unregisters_even_when_generation_raises():
    import asyncio

    from app.conversations import inflight

    async def main():
        inflight.reset()
        with pytest.raises(RuntimeError):
            async with inflight.track("t-y"):
                raise RuntimeError("boom")
        assert inflight.active("t-y") == 0
        inflight.reset()

    asyncio.run(main())


def test_inflight_wait_idle_times_out_and_still_proceeds():
    """等不到就返回 False(调用方照常往下删)—— 宁可多删一次,不留「以为删了」的正文。"""
    import asyncio

    from app.conversations import inflight

    async def main():
        inflight.reset()
        async with inflight.track("t-z"):
            assert await inflight.wait_idle("t-z", timeout=0.01) is False
        inflight.reset()

    asyncio.run(main())
