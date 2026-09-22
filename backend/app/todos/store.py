"""待办存储:单个 Redis 字符串键存一份 JSON 数组,全局共享。

写操作用 WATCH/MULTI 乐观事务(读改写整份数组),避免两次近乎同时的编辑互相覆盖 ——
量级极小但这样是正确的,成本也低。读操作直接 GET,不加锁。

本模块只做「存」与「取」;接口层的降级(enabled/degraded)在 routes/todos.py,
给 Agent 的渲染是模块级函数 format_for_prompt(供 chat 路由在调图前调用)。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from app.schemas.todo import Todo, TodoCreate, TodoUpdate

# 待办在 Redis 里的键;带版本后缀,便于日后结构升级时并存 / 迁移。
TODO_KEY = "qa:todos:v1"

# 注入进 system prompt 的未完成待办上限:防止待办过多时把提示撑爆。
PROMPT_MAX = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sorted(items: list[dict]) -> list[dict]:
    """展示序:未完成在前;组内按截止日期升序(无截止排最后);同组再按创建时间倒序(新在前)。

    依赖 Python sort 的稳定性:先按创建时间倒序打底,再按 (done, 无截止, 截止日期) 稳定排。
    """
    out = sorted(items, key=lambda t: t.get("created_at") or "", reverse=True)
    out.sort(key=lambda t: (bool(t.get("done")), t.get("due_date") is None, t.get("due_date") or ""))
    return out


class TodoStore:
    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def _read(self) -> list[dict]:
        raw = await self._redis.get(TODO_KEY)
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else []

    async def _mutate(self, fn: Callable[[list[dict]], tuple[Any, list[dict]]]) -> Any:
        """在 WATCH/MULTI 事务里读改写:fn(items) -> (返回值, 新数组);键被并发改动则自动重试。"""
        from redis.exceptions import WatchError

        async with self._redis.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(TODO_KEY)
                    raw = await pipe.get(TODO_KEY)
                    items = json.loads(raw) if raw else []
                    if not isinstance(items, list):
                        items = []
                    result, new_items = fn(items)
                    pipe.multi()
                    pipe.set(TODO_KEY, json.dumps(new_items, ensure_ascii=False))
                    await pipe.execute()
                    return result
                except WatchError:
                    continue  # 期间有别的写入,重来一次
                finally:
                    await pipe.reset()

    async def list(self) -> list[Todo]:
        """全部待办,按展示序。"""
        return [Todo(**t) for t in _sorted(await self._read())]

    async def list_open(self) -> list[dict]:
        """未完成待办(原始 dict,按展示序);供 chat 路由渲染给 Agent。"""
        return [t for t in _sorted(await self._read()) if not t.get("done")]

    async def add(self, create: TodoCreate) -> Todo:
        todo = Todo(
            id=uuid4().hex,
            title=create.title,
            category=create.category,
            done=False,
            created_at=_now_iso(),
            due_date=create.due_date,
        )

        def _fn(items: list[dict]) -> tuple[Todo, list[dict]]:
            return todo, [*items, todo.model_dump()]

        return await self._mutate(_fn)

    async def update(self, todo_id: str, patch: TodoUpdate) -> Todo | None:
        # 只取显式传入的字段(exclude_unset):None 也可能是「不改」而非「置空」。
        changes = patch.model_dump(exclude_unset=True)

        def _fn(items: list[dict]) -> tuple[Todo | None, list[dict]]:
            updated: Todo | None = None
            out: list[dict] = []
            for t in items:
                if t.get("id") == todo_id:
                    merged = {**t, **changes}
                    updated = Todo(**merged)
                    out.append(updated.model_dump())
                else:
                    out.append(t)
            return updated, (out if updated else items)

        return await self._mutate(_fn)

    async def delete(self, todo_id: str) -> bool:
        def _fn(items: list[dict]) -> tuple[bool, list[dict]]:
            out = [t for t in items if t.get("id") != todo_id]
            return (len(out) != len(items)), out

        return await self._mutate(_fn)

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:  # noqa: BLE001 —— 关闭失败无所谓,不阻塞关机
            pass


async def create_todo_store(url: str) -> TodoStore:
    """连接 Redis 并探活;失败则抛出(由 main.py 捕获后置 None、优雅降级)。"""
    from redis.asyncio import Redis

    # 连接加固与 checkpointer 一致(见 main.py 注释):公网空闲不被掐、复用前先探活。
    client = Redis.from_url(url, decode_responses=True, socket_keepalive=True, health_check_interval=30)
    await client.ping()
    return TodoStore(client)


def _due_note(due: str | None) -> str:
    """把截止日期渲染成人类可读后缀:已逾期 / 今天 / 还剩 N 天。"""
    if not due:
        return ""
    try:
        d = date.fromisoformat(due)
    except ValueError:
        return f",截止 {due}"
    delta = (d - date.today()).days
    if delta < 0:
        return f",截止 {due},已逾期 {-delta} 天"
    if delta == 0:
        return f",截止 {due},今天到期"
    return f",截止 {due},还剩 {delta} 天"


def format_for_prompt(open_items: list[dict]) -> str:
    """把未完成待办渲染成注入 system prompt 的一段中文;空列表返回空串(系统提示零变化)。"""
    if not open_items:
        return ""
    shown = open_items[:PROMPT_MAX]
    lines = [
        f"- {t.get('title', '')}({t.get('category') or '其他'}{_due_note(t.get('due_date'))})"
        for t in shown
    ]
    if len(open_items) > PROMPT_MAX:
        lines.append(f"- (另有 {len(open_items) - PROMPT_MAX} 项未列出)")
    body = "\n".join(lines)
    return (
        "\n\n【用户当前的未完成待办】"
        "(仅供参考:你不能增删改待办,只在与本次提问相关时主动提醒或结合作答;"
        "待办的管理入口在前端「待办清单」,不要声称自己能代为操作):\n"
        f"{body}"
    )
