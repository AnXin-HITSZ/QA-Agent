"""待办清单:审计字段(created_by / updated_by)、降级路径、给 Agent 的渲染。

用一个内存假 Redis 顶替真 Redis(WATCH/MULTI 的并发语义是 redis-py 的事,不是本文件
的主题);身份沿用 autouse 的「已登录管理员」桩 —— 写接口本来就只对管理员开放。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.schemas.todo import TodoCreate, TodoUpdate
from app.todos.store import PROMPT_MAX, TODO_KEY, TodoStore, format_for_prompt


class _FakePipeline:
    """够用的 WATCH/MULTI 管道:watch/get/multi/set/execute/reset(无并发,不会有 WatchError)。"""

    def __init__(self, redis) -> None:
        self._redis = redis
        self._pending: list[tuple[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def watch(self, key):
        return True

    async def get(self, key):
        return await self._redis.get(key)

    def multi(self):
        return None

    def set(self, key, value):
        self._pending.append((key, value))

    async def execute(self):
        self._redis._maybe_fail()
        for key, value in self._pending:
            self._redis.data[key] = value
        return len(self._pending)

    async def reset(self):
        self._pending = []


class FakeRedis:
    """只实现待办用到的命令;fail=True 时读写都抛**真** RedisError,走路由的降级分支。"""

    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})
        self.fail = False

    def _maybe_fail(self) -> None:
        if self.fail:
            from redis.exceptions import ConnectionError as RedisConnError

            raise RedisConnError("redis down")

    async def get(self, key):
        self._maybe_fail()
        return self.data.get(key)

    def pipeline(self):
        return _FakePipeline(self)

    async def aclose(self):
        return None


@pytest.fixture
def todo_store(monkeypatch):
    """装上内存 Redis 的待办存储,并挂进 app.state(路由就是从那里取的)。"""
    redis = FakeRedis()
    store = TodoStore(redis)
    monkeypatch.setattr(app.state, "todos", store, raising=False)
    return SimpleNamespace(redis=redis, store=store)


def _client() -> TestClient:
    return TestClient(app)


def _url(path: str) -> str:
    return f"{get_settings().api_prefix}{path}"


# 与 conftest.stub_principal 的默认身份一致(autouse 桩一直在演这个管理员)
STUB_ADMIN = {"user_id": "00000000-0000-4000-8000-00000000ad11",
              "display_name": "桩管理员", "email": "stub-admin@example.com"}


# ---- 审计:谁建的、谁改的 ----


def test_create_stamps_the_creator(todo_store):
    r = _client().post(_url("/todos"), json={"title": "报销差旅", "category": "报销"})
    assert r.status_code == 201
    body = r.json()
    assert body["created_by"] == STUB_ADMIN
    assert body["updated_by"] == STUB_ADMIN

    item = _client().get(_url("/todos")).json()["items"][0]
    assert item["created_by"]["display_name"] == "桩管理员"


def test_update_moves_updated_by_and_keeps_created_by(todo_store, _stub_auth):
    """换个人来改:updated_by 跟着换,created_by 保持**创建时**的那个人。"""
    from tests.conftest import stub_principal

    c = _client()
    todo_id = c.post(_url("/todos"), json={"title": "买耗材"}).json()["id"]

    _stub_auth.principal = stub_principal(user_id="00000000-0000-4000-8000-00000000beef",
                                          display_name="另一位管理员",
                                          email="other-admin@example.com")
    body = c.patch(_url(f"/todos/{todo_id}"), json={"done": True}).json()
    assert body["created_by"]["display_name"] == "桩管理员"
    assert body["updated_by"]["display_name"] == "另一位管理员"
    assert body["done"] is True and body["title"] == "买耗材"      # 局部更新语义没变


def test_legacy_rows_without_actors_still_work(todo_store):
    """字段引入前写下的行(没有这两个键)照常读、照常改 —— 但不**假造**一个创建者。"""
    todo_store.redis.data[TODO_KEY] = json.dumps(
        [{"id": "old", "title": "旧待办", "category": "", "done": False,
          "created_at": "2026-01-01T00:00:00+00:00", "due_date": None}], ensure_ascii=False)

    items = _client().get(_url("/todos")).json()["items"]
    assert items[0]["created_by"] is None and items[0]["updated_by"] is None

    body = _client().patch(_url("/todos/old"), json={"done": True}).json()
    assert body["created_by"] is None
    assert body["updated_by"]["display_name"] == "桩管理员"


def test_store_keeps_updated_by_when_called_without_an_actor(todo_store):
    """不带操作者调用(内部调用 / 测试)不该把上一次的审计信息抹掉。"""
    store = todo_store.store

    async def main():
        from tests.conftest import stub_principal

        from app.schemas.todo import TodoActor

        principal = stub_principal()
        actor = TodoActor(user_id=principal.user_id, display_name=principal.display_name,
                          email=principal.email)
        todo = await store.add(TodoCreate(title="甲"), actor)
        assert todo.updated_by == actor
        after = await store.update(todo.id, TodoUpdate(done=True))          # 不带 actor
        assert after.updated_by == actor                                     # 没被抹掉
        return after

    asyncio.run(main())


# ---- 降级:Redis 未启用 / 这次没响应 ----


def test_list_degrades_when_redis_fails(todo_store):
    todo_store.redis.fail = True
    assert _client().get(_url("/todos")).json() == {"enabled": True, "degraded": True, "items": []}


def test_writes_are_503_when_redis_fails(todo_store):
    todo_store.redis.fail = True
    c = _client()
    assert c.post(_url("/todos"), json={"title": "x"}).status_code == 503
    assert c.patch(_url("/todos/whatever"), json={"done": True}).status_code == 503
    assert c.delete(_url("/todos/whatever")).status_code == 503


def test_without_a_store_lists_disabled(monkeypatch):
    monkeypatch.setattr(app.state, "todos", None, raising=False)
    assert _client().get(_url("/todos")).json() == {"enabled": False, "degraded": False, "items": []}
    assert _client().post(_url("/todos"), json={"title": "x"}).status_code == 503


# ---- 展示序与给 Agent 的渲染 ----


def test_open_items_come_first_then_by_due_date(todo_store):
    store = todo_store.store

    async def main():
        await store.add(TodoCreate(title="无截止"))
        await store.add(TodoCreate(title="晚", due_date="2026-12-31"))
        early = await store.add(TodoCreate(title="早", due_date="2026-01-01"))
        await store.update(early.id, TodoUpdate(done=True))
        return [t.title for t in await store.list()]

    assert asyncio.run(main()) == ["晚", "无截止", "早"]


def test_format_for_prompt_caps_and_labels_unfinished_items():
    items = [{"title": f"待办 {i}", "category": "报销" if i else "", "due_date": None, "done": False}
             for i in range(PROMPT_MAX + 3)]
    text = format_for_prompt(items)
    assert "- 待办 0(未分类)" in text
    assert "- 待办 1(报销)" in text
    assert f"另有 {3} 项未列出" in text
    assert text.count("\n- ") == PROMPT_MAX + 1     # 只渲染上限条数 + 一行「另有」
    assert format_for_prompt([]) == ""              # 没有未完成待办时系统提示零变化


def test_due_note_renders_overdue_today_and_remaining_days():
    from datetime import date, timedelta

    today = date.today()
    note = lambda due: format_for_prompt([{"title": "A", "due_date": due}])   # noqa: E731
    assert "已逾期 2 天" in note((today - timedelta(days=2)).isoformat())
    assert "今天到期" in note(today.isoformat())
    assert "还剩 3 天" in note((today + timedelta(days=3)).isoformat())
    assert "截止 不是日期" in note("不是日期")      # 解析不了就原样带出,不炸
