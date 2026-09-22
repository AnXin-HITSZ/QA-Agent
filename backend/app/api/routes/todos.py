"""待办清单:全局一份的增删改查。

存储在后端 Redis(单用户、无鉴权,与历史对话同一取向):
- 列表 —— 读整份,未完成在前;
- 新建 / 更新 / 删除 —— 读改写整份(存储层用乐观事务)。

Redis 未启用(未配置)时:列表返回 enabled=false + 空列表,增删改返回 503。
已启用但本次读写失败(超时 / Redis 错误):列表 degraded=true,增删改 503。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.config import get_settings
from app.schemas.todo import Todo, TodoCreate, TodoList, TodoUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix=get_settings().api_prefix, tags=["todos"])


def _store(request: Request):
    """lifespan 注入的 TodoStore;Redis 未启用 / 初始化失败时为 None。"""
    return getattr(request.app.state, "todos", None)


def _redis_unavailable(exc: BaseException) -> bool:
    """exc 是否为 Redis 连接 / 读取类错误(超时、连不上)。

    与 routes/conversations.py 里同名判定同源:据此把「已启用但这次没响应」降级为
    可读状态(列表 degraded / 写 503),而非漏成裸 500,也不误报「未启用」。
    redis 惰性导入(未装则恒 False)。
    """
    try:
        from redis.exceptions import RedisError
    except ImportError:
        return False
    return isinstance(exc, RedisError)


@router.get("/todos", response_model=TodoList)
async def list_todos(request: Request) -> TodoList:
    """全部待办,未完成在前。Redis 未启用则 enabled=false。"""
    store = _store(request)
    if store is None:
        return TodoList(enabled=False, items=[])
    try:
        items = await store.list()
    except Exception as exc:  # noqa: BLE001 —— 仅降级 Redis 不可用,其余原样上抛
        if _redis_unavailable(exc):
            logger.warning("待办:Redis 读取失败,列表降级为暂不可用:%s", exc)
            return TodoList(enabled=True, degraded=True, items=[])
        raise
    return TodoList(enabled=True, items=items)


@router.post("/todos", response_model=Todo, status_code=status.HTTP_201_CREATED)
async def create_todo(body: TodoCreate, request: Request) -> Todo:
    """新建一条待办,返回带 id / 创建时间的完整记录。"""
    store = _store(request)
    if store is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="待办存储未启用(未配置 Redis)")
    try:
        return await store.add(body)
    except Exception as exc:  # noqa: BLE001
        if _redis_unavailable(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="待办服务暂时无响应,请稍后重试"
            ) from exc
        raise


@router.patch("/todos/{todo_id}", response_model=Todo)
async def update_todo(todo_id: str, body: TodoUpdate, request: Request) -> Todo:
    """局部更新(勾选完成 / 改标题 / 改分类 / 改截止);仅传入字段生效。"""
    store = _store(request)
    if store is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="待办存储未启用(未配置 Redis)")
    try:
        updated = await store.update(todo_id, body)
    except Exception as exc:  # noqa: BLE001
        if _redis_unavailable(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="待办服务暂时无响应,请稍后重试"
            ) from exc
        raise
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="待办不存在")
    return updated


@router.delete("/todos/{todo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_todo(todo_id: str, request: Request) -> Response:
    """删除一条待办。"""
    store = _store(request)
    if store is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="待办存储未启用(未配置 Redis)")
    try:
        existed = await store.delete(todo_id)
    except Exception as exc:  # noqa: BLE001
        if _redis_unavailable(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="待办服务暂时无响应,请稍后重试"
            ) from exc
        raise
    if not existed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="待办不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
