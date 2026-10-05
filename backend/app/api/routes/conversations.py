"""历史对话:列出 / 读取 / 删除**自己的**会话。

两类存储各管一段(见 app/conversations/store.py 的模块说明):
- **MySQL `conversations`** 是目录与授权凭据 —— 列表、分页、归属、删除状态机都看它,
  所以「别人的会话」根本不会出现在列表里,也读不到、删不掉(对外一律 404);
- **Redis checkpointer** 存正文,按线程 id 取用。

三条与旧版(单用户)不同的硬规则:
1. 列表走 MySQL 分页,**不扫全库**;内容检索只在有上限的最近窗口里做(SEARCH_WINDOW);
2. 归属判定在 MySQL 行上,不在线程 id 字符串上(那玩意客户端能自己拼);
3. 删除是状态机:active → deleting → (Redis 抹除) → deleted,中途失败留在 deleting,
   启动对账补删 —— 绝不「先删 Redis 再发现库里没删掉」,也绝不谎报成功。

Redis 未启用(降级为单轮)时:列表仍可用(目录在 MySQL 里)但条目不带消息数,
读取 / 删除返回 503。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app import conversations
from app.auth.deps import require_user
from app.auth.service import Principal
from app.config import get_settings
from app.conversations import inflight
from app.conversations.store import SEARCH_WINDOW
from app.skills.images import image_attachments
from app.schemas.conversation import (
    ConversationDetail,
    ConversationList,
    ConversationMatch,
    ConversationMessage,
    ConversationSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix=get_settings().api_prefix, tags=["conversations"],
                   dependencies=[Depends(require_user)])


def _not_found() -> HTTPException:
    """别人的会话、不存在的会话、正在删的会话 —— 对外**同一个** 404。

    不区分三者:区分就等于告诉调用方「这个 id 是存在的,只是不属于你」(存在性泄漏)。
    """
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                         detail={"code": "conversation_not_found", "message": "会话不存在"})


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
    turn: list = []
    for m in messages:
        mtype = getattr(m, "type", None)
        if mtype == "human":
            turn = []
        turn.append(m)
        text = _text(getattr(m, "content", "")).strip()
        if not text:
            continue
        if mtype == "human":
            out.append(ConversationMessage(role="user", content=text))
        elif mtype == "ai":
            # 工具调用前的铺垫不附图片;最终回答从本轮工具轨迹恢复旧引用。
            images = [] if getattr(m, "tool_calls", None) else image_attachments(turn)
            out.append(ConversationMessage(role="assistant", content=text, images=images))
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


_SNIPPET_PAD = 34  # 命中词前后各留的字数


def _terms(q: str) -> list[str]:
    """查询串按空白切成关键词;多个词是「都要出现」(AND),而不是任一出现。"""
    return q.split()


def _snippet(text: str, terms: list[str]) -> str:
    """截命中处上下文:先摊平换行,再在最早出现的关键词前后各留一段,截断端补 …。"""
    flat = text.replace("\n", " ")
    low = flat.lower()
    best: tuple[int, int] | None = None  # (位置, 词长)
    for t in terms:
        i = low.find(t.lower())
        if i >= 0 and (best is None or i < best[0]):
            best = (i, len(t))
    if best is None:
        return ""
    i, n = best
    start = max(0, i - _SNIPPET_PAD)
    end = min(len(flat), i + n + _SNIPPET_PAD)
    core = flat[start:end].strip()
    return f"{'…' if start > 0 else ''}{core}{'…' if end < len(flat) else ''}"


def _match_of(replay: list[ConversationMessage], terms: list[str]) -> ConversationMatch | None:
    """在一通对话里找关键词组:同一条消息里全部出现才算命中(跨消息不算,免得片段解释不了为什么命中)。

    命中条数 = 这样的消息条数;片段取自第一条命中消息。
    """
    hits = [m for m in replay if all(t.lower() in m.content.lower() for t in terms)]
    if not hits:
        return None
    return ConversationMatch(role=hits[0].role, snippet=_snippet(hits[0].content, terms), count=len(hits))


class _MemoryDown(RuntimeError):
    """这一次读 / 删 Redis 没成功(区别于「压根没配 Redis」)。"""


async def _read_thread(cp, thread_id: str) -> list[ConversationMessage]:
    """读一条线程并回放成 Q&A;Redis 未启用返回 None,读失败抛 _MemoryDown。"""
    if cp is None:
        return None  # type: ignore[return-value]  —— 由调用方按「单轮模式」处理
    try:
        tup = await cp.aget_tuple({"configurable": {"thread_id": thread_id}})
    except Exception as exc:  # noqa: BLE001
        if _redis_unavailable(exc):
            raise _MemoryDown(str(exc)) from exc
        raise
    return _replay(_messages_of(tup))


def _summary(row, replay: list[ConversationMessage] | None, terms: list[str]) -> ConversationSummary:
    """目录行 + 回放文本 → 列表项。标题以目录里的为准(建会话时写的首条提问)。"""
    title = (row.title or "").strip() or "新对话"
    return ConversationSummary(
        thread_id=row.thread_id,
        title=title,
        message_count=len(replay) if replay else 0,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        match=_match_of(replay, terms) if (replay and terms) else None,
    )


@router.get("/conversations", response_model=ConversationList)
async def list_conversations(
    request: Request,
    q: str = Query(default="", max_length=100, description="按内容检索(空 = 不过滤)"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=30, ge=1, le=50),
    principal: Principal = Depends(require_user),
) -> ConversationList:
    """列**自己的**会话:MySQL 分页(最近活跃在前),每页再按需取回放文本。

    为什么这样分工:目录查询走 (user_id, status, updated_at) 索引,天然只含本人的会话,
    且分页是真分页(不再扫全库);正文只有当前这一页的 ≤limit 条才去 Redis 读,读不到
    也不影响列表本身 —— 目录在 MySQL 里,Redis 抖动时列表照常给出(标 degraded)。

    带 q 时改用有界检索:在最近 SEARCH_WINDOW 条自己的会话里找,命中才返回(见 _search)。
    """
    terms = _terms(q)
    if terms:
        return await _search(request, principal, terms, offset, limit)

    cp = _checkpointer(request)
    rows, total = await conversations.list_for_user(user_id=principal.user_id,
                                                   offset=offset, limit=limit)
    items: list[ConversationSummary] = []
    degraded = False
    for row in rows:
        try:
            replay = await _read_thread(cp, row.thread_id)
        except _MemoryDown as exc:
            logger.warning("历史对话:Redis 读取失败,该页正文暂缺:%s", exc)
            degraded = True
            replay = None
        items.append(_summary(row, replay, []))
    return ConversationList(enabled=cp is not None, degraded=degraded, items=items,
                            total=total, offset=offset, limit=limit)


async def _search(request: Request, principal: Principal, terms: list[str],
                  offset: int, limit: int) -> ConversationList:
    """在自己的会话里按内容检索:窗口有上限,不做全库扫描。

    窗口 = 最近 SEARCH_WINDOW 条(目录序),命中后在内存里分页 —— 所以 total 是**本窗口内**
    的命中数,不是历史全量的。这一点对前端是可见的(它只用来显示「找到 N 条」),
    真要全量检索得建索引,不在本期范围(见技术方案 §7)。
    """
    cp = _checkpointer(request)
    rows, _ = await conversations.list_for_user(user_id=principal.user_id,
                                               offset=0, limit=SEARCH_WINDOW)
    hits: list[ConversationSummary] = []
    degraded = False
    for row in rows:
        try:
            replay = await _read_thread(cp, row.thread_id)
        except _MemoryDown as exc:
            logger.warning("历史对话:检索时 Redis 读取失败,结果不完整:%s", exc)
            degraded = True
            continue
        if not replay:
            continue
        summary = _summary(row, replay, terms)
        if summary.match is not None:
            hits.append(summary)
    page = hits[offset:offset + limit]
    return ConversationList(enabled=cp is not None, degraded=degraded, items=page,
                            total=len(hits), offset=offset, limit=limit)


@router.get("/conversations/{thread_id}", response_model=ConversationDetail)
async def get_conversation(thread_id: str, request: Request,
                           principal: Principal = Depends(require_user)) -> ConversationDetail:
    """读自己某条会话的正文。别人的 / 不存在的 / 正在删的,一律 404。"""
    row = await conversations.owned_by_thread(thread_id=thread_id, user_id=principal.user_id)
    if row is None:
        raise _not_found()
    try:
        replay = await _read_thread(_checkpointer(request), thread_id)
    except _MemoryDown as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "memory_unavailable", "message": "记忆服务暂时无响应,请稍后重试"},
        ) from exc
    # 目录里有、Redis 里还没有(刚建好就被中断):当成「暂无消息」,不是 404 —— 会话确实存在。
    return ConversationDetail(thread_id=thread_id, messages=replay or [])


@router.delete("/conversations/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(thread_id: str, request: Request,
                              principal: Principal = Depends(require_user)) -> Response:
    """删除自己的会话:MySQL 状态机 → 等在途生成收尾 → 抹掉 Redis → 落墓碑。

    顺序不能反:先把目录置为 deleting,新请求当场就进不来了(chat 会 404);这之后再抹
    Redis。中途失败(Redis 不可用)如实回 503,行留在 deleting 由启动对账补删 ——
    绝不先删正文再假装目录也删了。
    """
    row = await conversations.owned_by_thread(thread_id=thread_id, user_id=principal.user_id)
    if row is None:
        raise _not_found()
    state = await conversations.begin_delete(conv_id=row.id, user_id=principal.user_id)
    if state is None:                     # 归属在这一刻变了(并发删除 / 改归属)→ 仍按 404
        raise _not_found()

    cp = _checkpointer(request)
    if cp is None:
        # 单轮模式:没有正文可删,目录直接落墓碑(缩进到 deleted,不留 deleting 悬挂行)。
        await conversations.finish_delete(conv_id=row.id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    await inflight.wait_idle(thread_id)
    try:
        await conversations.delete_thread_data(cp, thread_id)
    except Exception as exc:  # noqa: BLE001
        if _redis_unavailable(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "memory_unavailable",
                        "message": "记忆服务暂时无响应:会话已不可访问,正文删除将在后台补完"},
            ) from exc
        raise
    await conversations.finish_delete(conv_id=row.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
