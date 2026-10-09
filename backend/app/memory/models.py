"""长期记忆的领域模型：数据类、状态常量、正文规范化。

这一层不碰数据库、不碰网络，纯函数与数据结构 —— 于是「同一条事实是否重复」这类
判断可以在单元测试里直接钉住，不依赖任何外部服务。

与 Mem0（v2.2.1，Apache-2.0）的对应关系：本模块对应其 `mem0/memory/main.py` 里
「记忆条目（payload['data'] + hash + created_at/updated_at）」的形态，但存储结构是
自己设计的：MySQL 是事实源（见 app/memory/tables.py），Qdrant 只是可重建索引。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---- 记忆条目 ----

STATUS_ACTIVE = "active"
STATUS_DELETED = "deleted"

ORIGIN_LLM = "llm"      # 会话提取产生
ORIGIN_USER = "user"    # 用户在「我的记忆」里手添

# `origin` 记的是**这条记忆最初怎么来的**，改写不会改变它 —— 用户编辑了一条会话提取的
# 记忆、或维护决策改写了用户手添的记忆，来源都还是当初那个。否则同一行会在「llm / user」
# 之间来回跳，界面上就成了假信息；「最后一次是谁改的」看审计（memory_history.actor）。

# ---- 审计事件 ----

EVENT_ADD = "ADD"
EVENT_UPDATE = "UPDATE"
EVENT_DELETE = "DELETE"

ACTOR_LLM = "llm"
ACTOR_USER = "user"
ACTOR_SYSTEM = "system"   # 重建索引 / 数据清理等系统动作

# ---- 任务 ----

JOB_KIND_EXTRACT = "extract"

JOB_PENDING = "pending"      # 待执行（含退避等待）
JOB_RUNNING = "running"      # 已被认领，租约持有中
JOB_SUCCEEDED = "succeeded"  # 终态
JOB_FAILED = "failed"        # 终态（重试次数用尽，或不可重试的错误）

TERMINAL_JOB_STATUSES = (JOB_SUCCEEDED, JOB_FAILED)

# ---- 作用域（正式 / 评测）----

SCOPE_FORMAL = ""                # 正式数据：聊天产生的记忆与任务
SCOPE_LIMIT = 64                 # 与 memory_items.scope / memory_jobs.scope 的列宽一致


def eval_scope(run_id: str) -> str:
    """评测作用域标识：'eval:<run-id>'。

    评测与正式数据用同一套表、同一套代码，隔离靠这一列（不用独立的库 / 进程级全局开关）：
    普通 Worker 只认 scope=''、只对 scope='' 的数据补索引；评测 Worker 只认自己那一个
    'eval:<run-id>'。于是「评测 Worker 不认领正式任务、正式 Worker 不碰评测数据」是
    SQL 条件保证的，不依赖「运行时记得别开正式 Worker」。
    """
    rid = (run_id or "").strip()
    if not rid:
        raise ValueError("run-id 不能为空:作用域会与正式数据撞上")
    return f"eval:{rid}"[:SCOPE_LIMIT]


# ---- 删除 / 清理台账（memory_ops）----

OP_DELETE_VECTOR = "delete_vector"   # 按 memory_id 清掉它的全部向量点（含历史版本）
OP_PURGE_USER = "purge_user"         # 按用户 + 代次清掉该用户的向量点
OP_DELETE_OLD_VERSIONS = "delete_old_versions"
OP_KINDS = (OP_DELETE_VECTOR, OP_PURGE_USER, OP_DELETE_OLD_VERSIONS)

# ---- 事实类型（kind）----

KIND_PREFERENCE = "preference"
KIND_PROFILE = "profile"
KIND_TASK = "task"
KIND_EVENT = "event"
FACT_KINDS = (KIND_PREFERENCE, KIND_PROFILE, KIND_TASK, KIND_EVENT)


@dataclass(frozen=True)
class MemoryItem:
    """一条记忆（与 memory_items 行一一对应；dataclass 而不是 ORM 行，便于跨层传递）。"""

    id: str
    user_id: str
    text: str
    content_hash: str
    status: str
    origin: str
    thread_id: str | None
    revision: int
    embedding_version: str
    indexed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    # ---- 0005 增量字段（带默认值：既有调用方 / 测试按位置构造时不受影响）----
    scope: str = SCOPE_FORMAL
    kind: str = ""                    # preference / profile / task / event；空 = 未标注
    event_time: datetime | None = None  # 事件发生时间；未知为 NULL（不由系统推断）
    generation: int = 0               # 写入时的记忆代次
    meta_version: int = 0             # 元数据版本（仅非正文变更 +1）
    indexed_revision: int = 0         # 已写入索引的正文版本（0 = 从未索引）
    indexed_meta_version: int = 0     # 已写入索引的元数据版本
    fact_context: dict = field(default_factory=dict)  # 时间、状态与消息来源（0008）

    @property
    def active(self) -> bool:
        return self.status == STATUS_ACTIVE

    def index_synced(self, embedding_version: str) -> bool:
        """这条记忆的向量是否**真的**与当前事实一致（三个条件缺一不可）。

        只看 embedding_version 会漏掉「同一个模型下正文改过、索引还是旧的」——那正是
        发现不了的自愈死角（见 repo.pending_index_ids）。
        """
        return (self.embedding_version == embedding_version
                and int(self.indexed_revision) == int(self.revision)
                and int(self.indexed_meta_version) == int(self.meta_version))

    def vector_is_current(self, embedding_version: str) -> bool:
        """向量本体是否还是这一版正文的（只看正文与模型口径，不看元数据）。

        元数据变了不必重新向量化：正文没变就复用已有向量，只刷新索引里的 payload。
        """
        return (self.embedding_version == embedding_version
                and int(self.indexed_revision) == int(self.revision))


@dataclass(frozen=True)
class MemoryHistory:
    """一条变更审计。"""

    id: int
    memory_id: str
    user_id: str
    event: str
    old_text: str | None
    new_text: str | None
    actor: str
    reason: str
    thread_id: str | None
    created_at: datetime
    old_context: dict | None = None
    new_context: dict | None = None


@dataclass(frozen=True)
class MemoryJob:
    """一个后台任务。payload 只在 pending / running 期间有内容，终态清成 {}。"""

    id: str
    user_id: str
    kind: str
    status: str
    dedupe_key: str
    thread_id: str | None
    payload: dict
    generation: int
    attempts: int
    max_attempts: int
    next_run_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error: str
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    # ---- 0005 增量字段 ----
    scope: str = SCOPE_FORMAL
    turn_id: str | None = None       # 本任务对应的稳定轮次标识（来源关联的幂等键）
    claim_token: str = ""            # 本次认领的凭证：收尾 / 续租 / 落库前都要一致
    stages: dict = field(default_factory=dict)   # 阶段结果（输入摘要 / 协议 / 模型 / 状态）
    outcome: dict | None = None      # 执行结果摘要（计数与标记，不含正文）
    committed_at: datetime | None = None         # 非空 = 事实已提交

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES


@dataclass(frozen=True)
class MemorySource:
    """一条记忆的来源轮次（memory_sources 行）。"""

    id: int
    memory_id: str
    user_id: str
    scope: str
    conversation_id: str | None
    turn_id: str
    created_at: datetime


@dataclass(frozen=True)
class MemoryOp:
    """一条删除 / 清理操作（memory_ops 行）。"""

    id: str
    user_id: str
    scope: str
    kind: str
    memory_id: str | None
    generation: int
    payload: dict
    status: str
    attempts: int
    max_attempts: int
    next_run_at: datetime
    lease_owner: str | None
    claim_token: str
    lease_expires_at: datetime | None
    last_error: str
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


@dataclass
class ExtractionOutcome:
    """一次提取 + 维护的整体结果（供接口 / 测试断言，不进库）。"""

    added: list[MemoryItem] = field(default_factory=list)
    updated: list[MemoryItem] = field(default_factory=list)
    deleted: list[MemoryItem] = field(default_factory=list)
    skipped: int = 0            # NOOP（已有相同事实）条数
    dropped: list[str] = field(default_factory=list)   # 被丢弃条目的原因（只记原因，不记正文）

    @property
    def changed(self) -> int:
        return len(self.added) + len(self.updated) + len(self.deleted)


# ---- 正文规范化 ----

_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """规范化记忆正文：Unicode NFKC + 折叠空白 + 去首尾。

    只做「不改变语义」的归一（全角/半角、连续空白的写法差异），不删除标点、不改大小写 ——
    正文最终要展示给用户，规范化只在**算摘要**这一步用，不覆写原文。
    """
    t = unicodedata.normalize("NFKC", text or "")
    return _WS.sub(" ", t).strip()


def content_hash(text: str) -> str:
    """规范化正文的 MD5 摘要（与 Mem0 的 payload['hash'] 同用途：同用户内去重）。"""
    return hashlib.md5(normalize_text(text).encode("utf-8")).hexdigest()


def fingerprint(*parts: object) -> str:
    """把若干标识 / 输入拼成一个稳定摘要（阶段结果「还能不能复用」的判据）。

    用途是**比较**,不是防篡改:中间结果要不要复用,取决于「输入、协议、模型口径」这三样
    有没有变。摘要本身可以安全入库 / 入日志(它是输入与协议的哈希,不含正文)。
    """
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def clamp_text(text: str, limit: int) -> str:
    """截断正文到上限（按字符数；超长说明模型没按「一句事实」输出，宁可截断也不入库超长文本）。"""
    t = (text or "").strip()
    if limit > 0 and len(t) > limit:
        return t[:limit]
    return t


# ---- 事件时间 ----

# 只认「写全了的」日期 / 日期时间：用户说「下周三」「上个月」这类相对说法时模型不该
# 换算成具体日期（那是推断），协议里也不接受 —— 解析不了就按未知处理（NULL）。
_EVENT_TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
                       "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d")


def parse_event_time(value) -> datetime | None:
    """把协议里的 event_time 解析成 datetime（解析不了返回 None，**不推断、不顶替**）。

    事件时间是「这件事什么时候发生的」，与记录时间（created_at）是两回事；未知就是 None
    （库里 NULL），绝不拿 created_at 顶替。只精确到日时时间部分为 00:00（UTC），
    这一点在技术方案里写明：它只用于展示与排序，不参与任何判断。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        text = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            dt = None
        if dt is None:
            for fmt in _EVENT_TIME_FORMATS:
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
    if dt.tzinfo is not None:
        # 带时区的写法是明确的（模型照抄用户原话里的偏移量），换算成 UTC 再存
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
