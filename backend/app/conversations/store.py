"""会话目录:MySQL 记「谁的、叫什么、还在不在」,Redis 存正文。

分工(技术方案 §7):
- **MySQL `conversations`** 是会话的目录**与授权凭据**:列表、分页、归属判断、删除状态机
  都看它。「这通对话是谁的」由表的 user_id 列回答,不由客户端递过来的字符串回答。
- **Redis(checkpointer)** 只存正文。线程 id 里虽然也嵌了 user_id,但那是**寻址**用的,
  不是权限 —— 任何人都能自己拼一个别人的线程 id 发过来,所以每次都要回 MySQL 核对归属。

线程 id 形状:`qa:{env}:chat:v1:u:{user_id}:c:{conversation_id}`。四段各有用途:
- env:同一台 Redis 上开发 / 生产数据天然隔离(共用一个实例时尤其重要);
- chat:v1:消息结构换代时整体换命名空间,不与旧线程混读;
- u / c:可寻址、可审计,也便于按用户批量清理。

**分页与检索都是有界的**:列表走 MySQL 的 (user_id, status, updated_at) 索引分页,
不扫全库;内容检索只在一个有上限的最近窗口里做(见 SEARCH_WINDOW),不做全量扫描。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from app.auth import db, store
from app.config import get_settings

THREAD_ROOT = "qa"
THREAD_SCOPE = "chat"
THREAD_VERSION = "v1"

MAX_TITLE_LENGTH = 100          # 与 conversations.title 的列宽一致
SEARCH_WINDOW = 200             # 内容检索最多回看多少个会话(有界扫描,不是全库)
DELETE_RECONCILE_SECONDS = 60   # 卡在 deleting 超过这么久,视为「上次没删完」


@dataclass(frozen=True)
class ConversationRef:
    """conversations 表里的一行的只读快照(路由层用它,不直接用 ORM 对象)。"""

    id: str
    user_id: str
    thread_id: str
    title: str
    status: str
    updated_at: datetime


def _ref(row) -> ConversationRef:
    return ConversationRef(id=row.id, user_id=row.user_id, thread_id=row.thread_id,
                           title=row.title, status=row.status, updated_at=row.updated_at)


def thread_id_for(*, user_id: str, conv_id: str, env: str | None = None) -> str:
    """拼线程 id。env 不传就取当前环境配置 —— 生产与开发各有自己的命名空间。"""
    scope = env if env is not None else get_settings().app_env
    return f"{THREAD_ROOT}:{scope}:{THREAD_SCOPE}:{THREAD_VERSION}:u:{user_id}:c:{conv_id}"


def parse_thread_id(thread_id: str) -> tuple[str, str] | None:
    """拆出 (user_id, conv_id);不是**本环境本版本**的线程 id 一律返回 None。

    用途只是「快速拒掉明显不属于这里的 id」(比如别的环境、上一代格式),**不用于授权**:
    这个字符串是客户端可伪造的,授权一律回 MySQL 看行归属。
    """
    parts = (thread_id or "").split(":")
    if len(parts) != 8:
        return None
    root, env, scope, version, uk, user_id, ck, conv_id = parts
    if (root, scope, version, uk, ck) != (THREAD_ROOT, THREAD_SCOPE, THREAD_VERSION, "u", "c"):
        return None
    if env != get_settings().app_env or not user_id or not conv_id:
        return None
    return user_id, conv_id


# ---- 同步内核(都短事务;异步包装见文件末尾)----


def _create_sync(*, user_id: str, title: str, now: datetime) -> ConversationRef:
    conv_id = str(uuid4())
    thread_id = thread_id_for(user_id=user_id, conv_id=conv_id)
    with db.session_scope() as session:
        store.create_conversation(session, conv_id=conv_id, user_id=user_id,
                                  thread_id=thread_id, title=(title or "")[:MAX_TITLE_LENGTH],
                                  now=now)
        return ConversationRef(id=conv_id, user_id=user_id, thread_id=thread_id,
                               title=(title or "")[:MAX_TITLE_LENGTH],
                               status=store.CONV_ACTIVE, updated_at=now)


def _owned_sync(*, thread_id: str, user_id: str) -> ConversationRef | None:
    with db.session_scope() as session:
        row = store.get_conversation_by_thread_id(session, thread_id)
        if row is None or row.user_id != user_id or row.status != store.CONV_ACTIVE:
            return None          # 不存在 / 不是你的 / 正在删 —— 对外都是 404
        return _ref(row)


def _list_sync(*, user_id: str, offset: int, limit: int) -> tuple[list[ConversationRef], int]:
    with db.session_scope() as session:
        rows, total = store.list_conversations(session, user_id=user_id,
                                               offset=offset, limit=limit)
        return [_ref(r) for r in rows], total


def _touch_sync(*, conv_id: str, title: str | None, now: datetime) -> bool:
    with db.session_scope() as session:
        return store.touch_conversation(session, conv_id=conv_id, now=now, title=title)


def _begin_delete_sync(*, conv_id: str, user_id: str, now: datetime) -> tuple[str, str] | None:
    with db.session_scope() as session:
        return store.begin_delete(session, conv_id=conv_id, user_id=user_id, now=now)


def _finish_delete_sync(*, conv_id: str, now: datetime) -> bool:
    with db.session_scope() as session:
        return store.finish_delete(session, conv_id=conv_id, now=now)


def _stuck_deleting_sync(*, older_than_seconds: int, now: datetime) -> list[ConversationRef]:
    with db.session_scope() as session:
        rows = store.list_deleting(session, older_than=now - timedelta(seconds=older_than_seconds))
        return [_ref(r) for r in rows]


# ---- 异步包装:异步路由里不让同步 DB 阻塞事件循环 ----


async def create_for_user(*, user_id: str, title: str) -> ConversationRef:
    """开一通新会话:后端自己生成 id,**不接受**客户端指定(否则就成了「替别人建会话」)。"""
    return await db.run(_create_sync, user_id=user_id, title=title, now=db.utc_naive())


async def owned_by_thread(*, thread_id: str, user_id: str) -> ConversationRef | None:
    return await db.run(_owned_sync, thread_id=thread_id, user_id=user_id)


async def list_for_user(*, user_id: str, offset: int = 0, limit: int = 30):
    return await db.run(_list_sync, user_id=user_id, offset=offset, limit=limit)


async def touch(*, conv_id: str, title: str | None = None) -> bool:
    return await db.run(_touch_sync, conv_id=conv_id, title=title, now=db.utc_naive())


async def begin_delete(*, conv_id: str, user_id: str) -> tuple[str, str] | None:
    """active → deleting(原子)。返回 (归属用户, 当前状态);None = 不存在或不是他的。"""
    return await db.run(_begin_delete_sync, conv_id=conv_id, user_id=user_id, now=db.utc_naive())


async def finish_delete(*, conv_id: str) -> bool:
    """deleting → deleted(墓碑)。Redis 那一步成功之后才调。"""
    return await db.run(_finish_delete_sync, conv_id=conv_id, now=db.utc_naive())


async def delete_thread_data(checkpointer, thread_id: str) -> None:
    """抹掉 Redis 里的线程(正文 / 写入记录 / 指针),由调用方处理异常与降级。"""
    await checkpointer.adelete_thread(thread_id)


async def reconcile_deleting(checkpointer, *, older_than_seconds: int = DELETE_RECONCILE_SECONDS) -> int:
    """启动对账:把上次进程没删完(卡在 deleting)的会话删干净。

    「删一半」的两种成因都靠它收尾:进程在 adelete_thread 之前崩,或者删除请求发出时
    Redis 不可用(那种情况接口已经如实回了 503,行留在 deleting 等这里补)。
    只处理**足够旧**的行:正在被别的 worker 处理的删除不该被抢。
    """
    rows = await db.run(_stuck_deleting_sync, older_than_seconds=older_than_seconds,
                        now=db.utc_naive())
    done = 0
    for row in rows:
        await delete_thread_data(checkpointer, row.thread_id)
        if await finish_delete(conv_id=row.id):
            done += 1
    return done
