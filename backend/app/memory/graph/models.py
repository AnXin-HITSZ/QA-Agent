"""记忆图的领域数据类与规范化（纯函数，不碰库、不碰 Neo4j）。

与 app/memory/models.py 同一姿态:repo 把行映射成这里的不可变数据类,业务层只依赖它们;
规范化（名称 / 宾语 / 描述的**匹配键**）只用于比较与去重,绝不覆写原文 —— 原文要展示给
用户,也用于回答引用。

名称匹配键与记忆正文摘要的差别:实体名匹配必须**大小写不敏感**
（"Caroline" / "caroline" 是同一个名字的说法差异）,所以 name_key 在 normalize_text 的
NFKC + 折叠空白之外再做 casefold;正文摘要（memory.models.content_hash）不做 casefold ——
正文是句子,大小写是语义的一部分。
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import datetime

# ---- 状态口径（与 memory/models.py 的 STATUS_* 同一套词,不另造近义词）----

ENTITY_ACTIVE = "active"
ENTITY_MERGED = "merged"      # 并入其它实体（同名消歧的更正）;行保留,审计可查
ENTITY_DELETED = "deleted"    # 软删（维护删除 / 来源全部撤回后清理）

MENTION_RESOLVED = "resolved"    # 已绑定实体
MENTION_PENDING = "pending"      # 候选模糊:保留待定,交维护 / 人工裁决 —— 绝不强行绑定
MENTION_UNRESOLVED = "unresolved"  # 明确不绑定（如泛指 / 无可用实体）

RELATION_ACTIVE = "active"
RELATION_DELETED = "deleted"

FACT_RELATION = "relation"
FACT_EVENT = "event"
FACT_KINDS = (FACT_RELATION, FACT_EVENT)

# 事件状态与事实 state 同一口径（extract.py 的 state 枚举）,别在这里另起一套:
EVENT_STATES = ("unknown", "planned", "ongoing", "completed", "cancelled")

# 时间精度（与 temporal.py 的 precision 口径一致）:
TIME_PRECISIONS = ("year", "month", "day", "minute", "unknown")

# 常见实体类型（提示词里列举用;校验不写死 —— kind 是可扩展字符串,未知可以是 'unknown'）:
KNOWN_ENTITY_KINDS = ("person", "place", "org", "project", "product", "animal", "event", "topic", "unknown")

# 常见参与者角色（同样只是常见值,不写死校验）:
KNOWN_ROLES = ("subject", "participant", "companion", "organizer", "recipient", "giver", "other")


def name_key(text: str) -> str:
    """名称 / 短语的匹配键:NFKC + 折叠空白 + casefold 后的 MD5。

    只做「不改变语义」的归一（全角半角、空白、大小写）;不做同义词、不做拼音、不做词干 ——
    近似匹配是消歧决策（要带证据、允许待定）,不是哈希能代替的。
    """
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = " ".join(t.split())
    return hashlib.md5(t.encode("utf-8")).hexdigest()


# ---- 领域数据类（repo 行映射的目标;业务层只见这些）----


@dataclass(frozen=True)
class Entity:
    id: str
    user_id: str
    scope: str
    kind: str
    name: str
    normalized: str
    status: str
    generation: int
    revision: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class EntityAlias:
    id: int
    entity_id: str
    user_id: str
    scope: str
    alias: str
    normalized: str
    origin: str
    created_at: datetime


@dataclass(frozen=True)
class EntityMention:
    id: int
    entity_id: str | None
    user_id: str
    scope: str
    memory_id: str | None
    turn_key: str
    surface: str
    normalized: str
    speaker: str | None
    status: str
    evidence: dict
    generation: int
    created_at: datetime


@dataclass(frozen=True)
class Relation:
    id: str
    user_id: str
    scope: str
    subject_entity_id: str
    relation_type: str
    object_entity_id: str | None
    object_text: str
    object_normalized: str
    memory_id: str
    event_time: datetime | None
    state: str
    status: str
    generation: int
    revision: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class Event:
    id: str
    user_id: str
    scope: str
    kind: str
    description: str
    normalized: str
    time_expression: str
    anchor: datetime | None
    start_at: datetime | None
    end_at: datetime | None
    time_precision: str
    status: str
    attributes: dict
    memory_id: str
    generation: int
    revision: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class EventParticipant:
    id: int
    event_id: str
    user_id: str
    scope: str
    role: str
    entity_id: str | None
    name_text: str
    normalized: str
    created_at: datetime


@dataclass(frozen=True)
class FactLink:
    id: int
    fact_kind: str
    fact_id: str
    memory_id: str
    memory_revision: int
    user_id: str
    scope: str
    turn_key: str
    quote: str
    meta: dict
    generation: int
    created_at: datetime
