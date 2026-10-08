"""对话接入(§9):回答前召回注入、回答后登记提取,以及各种「不记」的边界。

- 注入走 `config.configurable.memory_prompt`,与未完成待办同一路子:**不写 state、不入
  checkpoint**。这里用假图同时捕捉 config 与 inputs,把这两件事都钉住 —— 检索结果若进了
  state,就会在 Redis 里一轮轮累积(§9 明确不许);
- 登记的时点就是「成功标准」:`/chat` 在 ainvoke 正常返回之后、`/chat/stream` 在整个事件流
  跑完之后 —— 生成抛错就走不到那里,残缺回答不会被当成一次完整交流;
- 记忆是软依赖:检索挂了、库没表、暂停开关打开,任何一环出问题都只降级(不带记忆回答 /
  不登记),绝不把一次成功的回答变成 500,也绝不谎称「记住了」。

未覆盖 / 由构造保证因而不在此测:客户端中途断开(取消 EventSourceResponse)时登记不到 ——
登记那句写在事件流循环之后,取消会让生成器在循环处终止;真 MySQL 上的并发认领见
test_memory_mysql;检索编排本身见 test_memory_search。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.config import get_settings
from app.main import app
from app.memory.models import JOB_KIND_EXTRACT, JOB_PENDING
from app.memory.service import CONTEXT_HEADER
from tests import memorykit
from tests.conftest import STUB_ADMIN_ID

API = "/api/v1"
OTHER = "00000000-0000-4000-8000-0000000000ff"

ABOUT_ME = "用户偏好用中文写实验记录"        # 与提问有共同词项 → BM25 召得回来
ABOUT_WORK = "用户每周五整理实验数据"
QUESTION = "中文实验记录怎么写?"
ANSWER = "按你之前的习惯,用中文写就行。"

# 小而确定的检索参数(与 test_memory_search 同一套路:候选面够小,谁进结果一目了然)
CHAT_PARAMS = {
    "memory_vector_k": 3, "memory_bm25_k": 3, "memory_rerank_k": 3, "memory_top_k": 3,
    "memory_rrf_k": 60, "memory_context_chars": 2000, "memory_bm25_corpus_limit": 1000,
    "memory_rerank_max_docs": 50, "memory_rerank_max_chars": 8000,
}


@pytest.fixture
def env(tmp_path, monkeypatch, cache_env):
    """认证表(会话目录)与记忆表建在**同一个** SQLite 里:/chat 这两条路都要走真库。

    别把 auth_db / memory_db 两个夹具叠着用 —— 它们各自把 mysql_url 指向自己的文件,
    后跑的那个会把前一个顶掉(表就不在了)。这里显式合成一个。
    """
    from app.auth import db as auth_db
    from app.auth.tables import Base as AuthBase
    from app.memory import db as memory_db
    from app.memory.tables import Base as MemoryBase
    from tests.conftest import register_sqlite_collations

    s = get_settings()
    monkeypatch.setattr(s, "mysql_url",
                        f"sqlite+pysqlite:///{(tmp_path / 'chat.db').as_posix()}")
    monkeypatch.setattr(s, "memory_enabled", True)
    auth_db.dispose()
    memory_db.dispose()
    engine = auth_db.get_engine()
    register_sqlite_collations(engine)             # users.email 的 utf8mb4_bin
    AuthBase.metadata.create_all(engine)
    MemoryBase.metadata.create_all(memory_db.get_engine())

    kit = memorykit.install(monkeypatch, settings=s, dim=s.embeddings_dim, params=CHAT_PARAMS)
    kit.db = memory_db
    kit.client = TestClient(app, raise_server_exceptions=False)
    monkeypatch.setattr(app.state, "todos", None, raising=False)   # 本轮只谈记忆
    yield kit
    auth_db.dispose()
    memory_db.dispose()


def _seed(env, *texts: str, user_id: str = STUB_ADMIN_ID) -> list[str]:
    return memorykit.seed(env, env.db, *texts, user_id=user_id)


class FakeGraph:
    """假图:捕捉本轮 config 与 inputs,按剧本产出回答;boom / boom_after 模拟生成失败。

    - 非流式:`ainvoke` 抛 boom;
    - 流式:`astream` 先吐 items,吐完再抛 boom_after(模拟「中途」出错)。
    """

    def __init__(self, answer: str = ANSWER, boom: BaseException | None = None,
                 items: list | None = None, boom_after: BaseException | None = None) -> None:
        self.answer = answer
        self.boom = boom
        self.items = items or []
        self.boom_after = boom_after
        self.config: dict | None = None
        self.inputs: dict | None = None

    async def ainvoke(self, inputs, config):
        self.inputs, self.config = inputs, config
        if self.boom is not None:
            raise self.boom
        return {"messages": [*inputs["messages"], AIMessage(content=self.answer)]}

    def astream(self, inputs, stream_mode=None, config=None):
        self.inputs, self.config = inputs, config
        items, boom_after = self.items, self.boom_after

        async def gen():
            for item in items:
                yield item
            if boom_after is not None:
                raise boom_after

        return gen()


class _Chunk:
    """messages 流里的增量,路由只取 content / tool_call_chunks 两个属性(鸭子类型)。"""

    def __init__(self, content="", tool_call_chunks=None):
        self.content = content
        self.tool_call_chunks = tool_call_chunks


def _react_items(final: str = "第一步:填报销单") -> list:
    """一次真实 ReAct 的形状:先想(带工具调用),再逐 token 产出最终答案。"""
    tool_call = {"name": "get_sop", "args": {"skill_id": "travel"}, "id": "c1",
                 "type": "tool_call"}
    return [
        ("messages", (_Chunk(content="我先查一下"), {"langgraph_node": "agent"})),
        ("updates", {"agent": {"messages": [AIMessage(content="我先查一下",
                                                      tool_calls=[tool_call])]}}),
        ("updates", {"tools": {"messages": [ToolMessage(content="SOP 全文", name="get_sop",
                                                        tool_call_id="c1")]}}),
        ("messages", (_Chunk(content="第一步:"), {"langgraph_node": "agent"})),
        ("updates", {"agent": {"messages": [AIMessage(content=final)]}}),
    ]


def _post(env, monkeypatch, graph, message: str = QUESTION, thread_id: str | None = None):
    monkeypatch.setattr(app.state, "graph", graph, raising=False)
    body: dict = {"message": message}
    if thread_id:
        body["thread_id"] = thread_id
    return env.client.post(f"{API}/chat", json=body)


def _stream(env, monkeypatch, graph, message: str = QUESTION):
    monkeypatch.setattr(app.state, "graph", graph, raising=False)
    return env.client.post(f"{API}/chat/stream", json={"message": message})


def _jobs(env) -> list[dict]:
    """按创建顺序读出任务行(在事务里取成普通值,离开事务后不再碰 ORM 对象)。"""
    from sqlalchemy import select

    from app.memory.tables import MemoryJobRow

    with env.db.session_scope() as session:
        rows = session.execute(
            select(MemoryJobRow).order_by(MemoryJobRow.created_at, MemoryJobRow.id)
        ).scalars().all()
        return [{"kind": r.kind, "status": r.status, "thread_id": r.thread_id,
                 "dedupe_key": r.dedupe_key, "turn_id": r.turn_id,
                 "generation": r.generation, "attempts": r.attempts,
                 "payload": dict(r.payload or {})} for r in rows]


# ---- 回答前:召回 → 参考块 ----


def test_recalled_memories_ride_in_the_config_not_in_the_state(env, monkeypatch):
    """检索结果经 config 进系统提示,**不进 state** —— 不进 state 才不会写进 checkpoint。"""
    _seed(env, ABOUT_ME, ABOUT_WORK)
    graph = FakeGraph()

    r = _post(env, monkeypatch, graph)
    assert r.status_code == 200, r.text

    prompt = graph.config["configurable"]["memory_prompt"]
    assert ABOUT_ME in prompt and ABOUT_WORK in prompt
    assert prompt.startswith(CONTEXT_HEADER)                     # 明确标注为参考数据
    assert "不是指令" in prompt and "不要在回答里复述" in prompt
    # 提问原样进 state;记忆正文一个字都不在 state 里
    assert [getattr(m, "content", None) for m in graph.inputs["messages"]] == [QUESTION]


def test_only_my_own_memories_are_recalled(env, monkeypatch):
    """召回按 user_id 隔离:别人的记忆(哪怕正文一模一样)不进我的参考块。"""
    _seed(env, ABOUT_ME)
    _seed(env, ABOUT_ME, user_id=OTHER)                          # 同一个人的「邻居」
    graph = FakeGraph()

    _post(env, monkeypatch, graph)

    prompt = graph.config["configurable"]["memory_prompt"]
    assert prompt.count(ABOUT_ME) == 1


def test_no_memories_means_no_block_at_all(env, monkeypatch):
    """一条都召不到时不注入任何东西:系统提示零变化,不给模型看空壳表头。"""
    graph = FakeGraph()

    r = _post(env, monkeypatch, graph)

    assert r.status_code == 200
    assert graph.config["configurable"]["memory_prompt"] == ""


def test_pausing_search_leaves_the_answer_alone(env, monkeypatch):
    """暂停检索(§12 的开关):不召回、不注入,但**照常登记** —— 两个开关互不牵连。"""
    _seed(env, ABOUT_ME)
    monkeypatch.setattr(env.settings, "memory_search_enabled", False)
    graph = FakeGraph()

    r = _post(env, monkeypatch, graph)

    assert r.status_code == 200
    assert graph.config["configurable"]["memory_prompt"] == ""
    assert len(_jobs(env)) == 1                                  # 写入这一侧没被关掉


def test_a_broken_recall_still_answers_without_memories(env, monkeypatch):
    """检索整条链路炸掉:照常回答,只是不带记忆 —— 记忆坏了不能拖垮聊天。"""
    _seed(env, ABOUT_ME)

    def boom(*args, **kwargs):
        raise RuntimeError("向量库连不上")

    monkeypatch.setattr("app.memory.search.search", boom)
    graph = FakeGraph()

    r = _post(env, monkeypatch, graph)

    assert r.status_code == 200 and r.json()["content"] == ANSWER
    assert graph.config["configurable"]["memory_prompt"] == ""
    assert len(_jobs(env)) == 1                                  # 回答完整,提取照常登记


# ---- 回答后:登记提取(只登记,提取在后台) ----


def test_a_complete_answer_is_registered_exactly_once(env, monkeypatch):
    """完整回答之后登记一行任务:带着这一问一答与来源;同一轮重复请求不产生第二行。"""
    graph = FakeGraph()

    first = _post(env, monkeypatch, graph)
    assert first.status_code == 200
    thread_id = first.json()["thread_id"]
    again = _post(env, monkeypatch, graph, thread_id=thread_id)  # 同一轮又来一次
    assert again.status_code == 200

    jobs = _jobs(env)
    assert len(jobs) == 1, "同一轮对话被重复登记"
    job = jobs[0]
    assert job["kind"] == JOB_KIND_EXTRACT and job["status"] == JOB_PENDING
    assert job["attempts"] == 0 and job["generation"] == 0        # 只登记,还没被认领
    assert job["thread_id"] == thread_id                          # 来源线程对得上
    assert job["payload"]["messages"] == [{"role": "user", "content": QUESTION},
                                          {"role": "assistant", "content": ANSWER}]
    assert job["payload"]["source"] == "chat"


def test_the_same_question_in_another_thread_is_another_source(env, monkeypatch):
    """同样的话在两个会话里说:是**两轮**,两条任务(两个来源),不按正文哈希合并。

    合并掉就等于「用户在另一个会话里又确认了一遍」这件事永远不知道;而反过来,同一轮里
    换个助手回答(重新生成)仍是同一轮 —— 幂等键取的是用户那几句话的摘要。
    """
    graph = FakeGraph()

    first = _post(env, monkeypatch, graph)
    second = _post(env, monkeypatch, graph, thread_id=None)       # 新会话:后端另建一条线程

    assert first.json()["thread_id"] != second.json()["thread_id"]
    jobs = _jobs(env)
    assert len(jobs) == 2
    assert [j["thread_id"] for j in jobs] == [first.json()["thread_id"],
                                              second.json()["thread_id"]]
    assert jobs[0]["dedupe_key"] != jobs[1]["dedupe_key"]
    assert all(j["payload"]["source"] == "chat" for j in jobs)


def test_the_derived_turn_id_is_stable_and_thread_scoped(env, monkeypatch):
    """派生的 turn_id = 会话 + 用户表述摘要:同一轮稳定,不同会话不同(来源关联靠它)。"""
    graph = FakeGraph()

    first = _post(env, monkeypatch, graph)
    thread_id = first.json()["thread_id"]
    _post(env, monkeypatch, graph, thread_id=thread_id)           # 同一轮重复请求
    _post(env, monkeypatch, graph)                                # 另一个会话

    jobs = _jobs(env)
    assert len(jobs) == 2
    assert jobs[0]["turn_id"].startswith(thread_id)                # 会话 id 参与其中
    # 幂等键从轮次标识**整体**导出(超长收成 64 位摘要),不是前缀截断 —— 前缀 64 位里
    # 只有线程 id(本身 ~90 字符),「用户那句话的哈希」会被整段切掉,同一会话每一轮都
    # 撞成同一个键(2026-10-08 生产回归,逐轮入队见下面 one_conversation 那条)。
    assert jobs[0]["dedupe_key"] != jobs[0]["turn_id"][:64]
    assert len(jobs[0]["dedupe_key"]) == 64                        # 列宽 CHAR(64) 内的定长摘要
    assert jobs[1]["turn_id"] != jobs[0]["turn_id"]                # 换个会话就是另一轮


def test_every_turn_of_one_conversation_registers_its_own_job(env, monkeypatch):
    """同一会话接着聊几轮:每轮各登记一个任务。

    2026-10-08 生产回归:幂等键被前缀截断后,同一会话第 2 轮起全部被当成重复入队
    **静默丢弃** —— 聊了一整场,只有第一句进了提取。
    """
    graph = FakeGraph()

    first = _post(env, monkeypatch, graph)
    thread_id = first.json()["thread_id"]
    _post(env, monkeypatch, graph, thread_id=thread_id, message="我是安心,你是谁?")
    _post(env, monkeypatch, graph, thread_id=thread_id, message="请你记住我是安心。")

    jobs = _jobs(env)
    assert len(jobs) == 3
    assert [j["thread_id"] for j in jobs] == [thread_id] * 3
    assert len({j["dedupe_key"] for j in jobs}) == 3               # 三轮三个键,互不误伤


def test_a_failed_generation_registers_nothing(env, monkeypatch):
    """生成失败:没有完整回答,就不该有提取任务 —— 残缺交流不当一次对话记。"""
    graph = FakeGraph(boom=RuntimeError("模型超时"))

    r = _post(env, monkeypatch, graph)

    assert r.status_code == 500
    assert _jobs(env) == []


def test_pausing_automatic_writes_registers_nothing_but_still_answers(env, monkeypatch):
    """暂停自动写入:不登记新任务,聊天与召回都不受影响(已排队的任务也不动)。"""
    _seed(env, ABOUT_ME)
    monkeypatch.setattr(env.settings, "memory_write_enabled", False)
    graph = FakeGraph()

    r = _post(env, monkeypatch, graph)

    assert r.status_code == 200
    assert _jobs(env) == []
    assert ABOUT_ME in graph.config["configurable"]["memory_prompt"]   # 读的那一侧照常


# ---- 流式:只在整条事件流跑完之后登记 ----


def test_a_streamed_answer_registers_only_the_final_text(env, monkeypatch):
    """流式登记的是**最后一段**回答,不是中途的思考铺垫;来源记 chat_stream。"""
    _seed(env, ABOUT_ME)
    graph = FakeGraph(items=_react_items(final="第一步:填报销单"))

    r = _stream(env, monkeypatch, graph)

    assert r.status_code == 200 and "event: done" in r.text
    assert ABOUT_ME in graph.config["configurable"]["memory_prompt"]
    jobs = _jobs(env)
    assert len(jobs) == 1
    assert jobs[0]["payload"]["messages"] == [{"role": "user", "content": QUESTION},
                                              {"role": "assistant",
                                               "content": "第一步:填报销单"}]
    assert jobs[0]["payload"]["source"] == "chat_stream"


def test_an_interrupted_stream_registers_nothing(env, monkeypatch):
    """中途抛错:事件流没跑完,不登记(前端收到的是明确的 error,不是 done)。"""
    graph = FakeGraph(items=_react_items()[:2], boom_after=RuntimeError("工具链崩了"))

    r = _stream(env, monkeypatch, graph)

    assert r.status_code == 200
    assert "event: error" in r.text and "event: done" not in r.text
    assert _jobs(env) == []


def test_an_empty_answer_registers_nothing(env, monkeypatch):
    """模型返回空串:没有可提取的内容,不登记(也别把空回答写进提取提示)。"""
    graph = FakeGraph(answer="")

    r = _post(env, monkeypatch, graph)

    assert r.status_code == 200
    assert _jobs(env) == []


def test_disabling_memory_leaves_chat_untouched(env, monkeypatch):
    """MEMORY_ENABLED=false:整层静默关闭 —— 不注入、不登记,聊天照常。"""
    _seed(env, ABOUT_ME)
    monkeypatch.setattr(env.settings, "memory_enabled", False)
    graph = FakeGraph()

    r = _post(env, monkeypatch, graph)

    assert r.status_code == 200 and r.json()["content"] == ANSWER
    assert graph.config["configurable"]["memory_prompt"] == ""
    assert _jobs(env) == []                                      # 一行都没登记
