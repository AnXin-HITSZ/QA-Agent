"""历史对话:扫描 Redis checkpointer,列出 / 读取 / 删除会话。

以后端 Redis 为准(单用户,无鉴权):
- 列表 —— 扫描 checkpointer 索引里的全部线程(``alist(None)``),按最近活跃排序;
- 读取 —— 取某线程最新状态(``aget_tuple``),回放成干净的 Q&A 文本;
- 删除 —— ``adelete_thread`` 连带清掉该线程在 Redis 的全部 checkpoint / writes / 指针,记忆彻底抹除。

Redis 未启用(降级为单轮)时:列表返回 enabled=false + 空列表,读取 / 删除返回 503。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.config import get_settings
from app.schemas.conversation import (
    ConversationDetail,
    ConversationList,
    ConversationMessage,
    ConversationSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix=get_settings().api_prefix, tags=["conversations"])


def _checkpointer(request: Request):
    """lifespan 注入的 AsyncRedisSaver;Redis 未启用(降级为单轮)时为 None。"""
    return getattr(request.app.state, "checkpointer", None)


def _redis_unavailable(exc: BaseException) -> bool:
    """exc 是否为 Redis 连接/读取类错误(超时、连不上、RediSearch 检索失败)。

    区别于"未配置"(checkpointer 为 None):这里记忆是启用的,只是这一次没响应 ——
    公网远程 Redis 尤其常见。据此把它降级为可读状态(列表 degraded / 读写 503),
    而不是漏成裸 500,也不误报成"未启用"。redis / redisvl 惰性导入(未装则恒 False)。
    """
    types: list[type] = []
    try:
        from redis.exceptions import RedisError  # 超时 / 连接错误的基类

        types.append(RedisError)
    except ImportError:
        pass
    try:
        from redisvl.exceptions import RedisSearchError  # FT.SEARCH 失败(alist 走这条)

        types.append(RedisSearchError)
    except ImportError:
        pass
    return bool(types) and isinstance(exc, tuple(types))


def _text(content) -> str:
    """把 LangChain 消息的 content 归一成纯文本(本场景基本是 str;兼容多模态分块)。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text", "")))
        return "".join(parts)
    return str(content or "")


def _replay(messages: list) -> list[ConversationMessage]:
    """把状态里的消息轨迹压成干净的 Q&A:用户提问 + 有正文的助手回答;工具 / 系统 / 半截消息跳过。"""
    out: list[ConversationMessage] = []
    for m in messages:
        mtype = getattr(m, "type", None)
        text = _text(getattr(m, "content", "")).strip()
        if not text:
            continue
        if mtype == "human":
            out.append(ConversationMessage(role="user", content=text))
        elif mtype == "ai":
            out.append(ConversationMessage(role="assistant", content=text))
    return out


def _title(messages: list) -> str:
    """标题取首条用户提问,截断到 40 字。"""
    for m in messages:
        if getattr(m, "type", None) == "human":
            t = _text(getattr(m, "content", "")).strip()
            if t:
                return (t[:40] + "…") if len(t) > 40 else t
    return "新对话"


def _messages_of(tup) -> list:
    """从 CheckpointTuple 取状态里的 messages 通道(即我们 ChatState 的会话载体)。"""
    if tup is None:
        return []
    return (tup.checkpoint.get("channel_values") or {}).get("messages") or []


@router.get("/conversations", response_model=ConversationList)
async def list_conversations(request: Request) -> ConversationList:
    """扫描 checkpointer 里的全部线程,按最近活跃排序列出摘要。Redis 未启用则 enabled=false。"""
    cp = _checkpointer(request)
    if cp is None:
        return ConversationList(enabled=False, items=[])

    items: list[ConversationSummary] = []
    seen: set[str] = set()
    # alist(None) 返回所有线程的 checkpoint,按 checkpoint_id(ULID,内含时间)倒序 ——
    # 首次见到某线程即其最新状态,线程本身也随之「最近活跃在前」自然成序。
    try:
        async for tup in cp.alist(None):
            tid = tup.config["configurable"]["thread_id"]
            if tid in seen:
                continue
            seen.add(tid)
            messages = _messages_of(tup)
            replay = _replay(messages)
            if not replay:
                continue  # 只有系统 / 半截消息、无可展示内容的空壳线程不列
            items.append(
                ConversationSummary(
                    thread_id=tid,
                    title=_title(messages),
                    message_count=len(replay),
                    updated_at=tup.checkpoint.get("ts"),
                )
            )
    except Exception as exc:  # noqa: BLE001 —— 仅降级 Redis 不可用,其余原样上抛
        if _redis_unavailable(exc):
            logger.warning("历史对话:Redis 读取失败,列表降级为暂不可用:%s", exc)
            return ConversationList(enabled=True, degraded=True, items=[])
        raise
    return ConversationList(enabled=True, items=items)


@router.get("/conversations/{thread_id}", response_model=ConversationDetail)
async def get_conversation(thread_id: str, request: Request) -> ConversationDetail:
    """读取某线程最新状态,回放成 Q&A 文本序列。"""
    cp = _checkpointer(request)
    if cp is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="跨轮记忆未启用(未配置 Redis)")
    try:
        tup = await cp.aget_tuple({"configurable": {"thread_id": thread_id}})
    except Exception as exc:  # noqa: BLE001
        if _redis_unavailable(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="记忆服务暂时无响应,请稍后重试"
            ) from exc
        raise
    if tup is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在")
    return ConversationDetail(thread_id=thread_id, messages=_replay(_messages_of(tup)))


@router.delete("/conversations/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(thread_id: str, request: Request) -> Response:
    """删除某线程,连带清掉它在 Redis 的全部 checkpoint / writes / 指针。"""
    cp = _checkpointer(request)
    if cp is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="跨轮记忆未启用(未配置 Redis)")
    try:
        await cp.adelete_thread(thread_id)
    except Exception as exc:  # noqa: BLE001
        if _redis_unavailable(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="记忆服务暂时无响应,请稍后重试"
            ) from exc
        raise
    return Response(status_code=status.HTTP_204_NO_CONTENT)
