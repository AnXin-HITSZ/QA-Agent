"""记忆图的全部 SQL（MySQL 事实源;同步、短事务,与 app/memory/repo.py 同一姿态）。

- 会话由调用方管理（`with db.session_scope() as session:`）;事务里绝不发生外部调用;
- 一切读写都带 user_id + scope:隔离是数据层的事,不靠上层自觉;
- **同名 ≠ 同一实体**:按名字查实体返回**列表**（可能 0..n 个）;消歧决策在业务层,
  这里只如实存取;提及 / 参与者的 entity_id 可为 NULL（待定 / 未消歧）;
- 幂等:重复登记别名 / 提及 / 参与者 / 事实来源靠唯一键 + SAVEPOINT（撞键只回滚这一条,
  不带倒同事务的其它写入 —— 与 memory/repo.add_source 同一写法）;
- 版本:实体 / 关系 / 事件的 revision 在**内容变更**时 +1（对账 / 投影核验的依据）;
  清除按 generation 过滤的语义与记忆一致（旧代次的写回被 assert_generation 挡掉,
  这里再带上 generation 过滤删除）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.memory.graph.models import (
    ENTITY_ACTIVE, FACT_KINDS, Event, EventParticipant, Entity, EntityAlias, EntityMention,
    FactLink, Relation, name_key,
)
from app.memory.graph.tables import (
    MemoryEntityAliasRow, MemoryEntityMentionRow, MemoryEntityRow, MemoryEventParticipantRow,
    MemoryEventRow, MemoryFactLinkRow, MemoryRelationRow,
)
from app.memory.models import content_hash
from app.memory.repo import new_id

TURN_KEY_LIMIT = 160
_NAME_LIMIT = 500          # 名称类字段的保险上限（模型产出超长名称时截断,不整批失败)


# ---- 行 → 领域对象 ----

def _entity(row: MemoryEntityRow) -> Entity:
    return Entity(id=row.id, user_id=row.user_id, scope=row.scope, kind=row.kind,
                  name=row.name, normalized=row.normalized, status=row.status,
                  generation=row.generation, revision=row.revision,
                  created_at=row.created_at, updated_at=row.updated_at, deleted_at=row.deleted_at)


def _alias(row: MemoryEntityAliasRow) -> EntityAlias:
    return EntityAlias(id=row.id, entity_id=row.entity_id, user_id=row.user_id, scope=row.scope,
                       alias=row.alias, normalized=row.normalized, origin=row.origin,
                       created_at=row.created_at)


def _mention(row: MemoryEntityMentionRow) -> EntityMention:
    return EntityMention(id=row.id, entity_id=row.entity_id, user_id=row.user_id,
                         scope=row.scope, memory_id=row.memory_id, turn_key=row.turn_key,
                         surface=row.surface, normalized=row.normalized, speaker=row.speaker,
                         status=row.status, evidence=row.evidence or {}, generation=row.generation,
                         created_at=row.created_at)


def _relation(row: MemoryRelationRow) -> Relation:
    return Relation(id=row.id, user_id=row.user_id, scope=row.scope,
                    subject_entity_id=row.subject_entity_id, relation_type=row.relation_type,
                    object_entity_id=row.object_entity_id, object_text=row.object_text,
                    object_normalized=row.object_normalized, memory_id=row.memory_id,
                    event_time=row.event_time, state=row.state, status=row.status,
                    generation=row.generation, revision=row.revision,
                    created_at=row.created_at, updated_at=row.updated_at, deleted_at=row.deleted_at)


def _event(row: MemoryEventRow) -> Event:
    return Event(id=row.id, user_id=row.user_id, scope=row.scope, kind=row.kind,
                 description=row.description, normalized=row.normalized,
                 time_expression=row.time_expression, anchor=row.anchor, start_at=row.start_at,
                 end_at=row.end_at, time_precision=row.time_precision, status=row.status,
                 attributes=row.attributes or {}, memory_id=row.memory_id,
                 generation=row.generation, revision=row.revision, created_at=row.created_at,
                 updated_at=row.updated_at, deleted_at=row.deleted_at)


def _participant(row: MemoryEventParticipantRow) -> EventParticipant:
    return EventParticipant(id=row.id, event_id=row.event_id, user_id=row.user_id,
                            scope=row.scope, role=row.role, entity_id=row.entity_id,
                            name_text=row.name_text, normalized=row.normalized,
                            created_at=row.created_at)


def _link(row: MemoryFactLinkRow) -> FactLink:
    return FactLink(id=row.id, fact_kind=row.fact_kind, fact_id=row.fact_id,
                    memory_id=row.memory_id, memory_revision=row.memory_revision,
                    user_id=row.user_id, scope=row.scope, turn_key=row.turn_key,
                    quote=row.quote, meta=row.meta or {}, generation=row.generation,
                    created_at=row.created_at)


def _clip(text: str, limit: int = _NAME_LIMIT) -> str:
    t = (text or "").strip()
    return t[:limit]


# ---- 实体 ----

def create_entity(session, *, user_id: str, scope: str, kind: str, name: str,
                  generation: int, now: datetime) -> Entity:
    """新建实体（调用方负责消歧决策:什么时候该**复用**已有实体、什么时候是新的同名实体）。"""
    name = _clip(name)
    row = MemoryEntityRow(
        id=new_id(), user_id=user_id, scope=scope, kind=_clip(kind, 24) or "unknown",
        name=name, normalized=name_key(name), status=ENTITY_ACTIVE,
        generation=int(generation), revision=1, created_at=now, updated_at=now,
    )
    session.add(row)
    session.flush()
    return _entity(row)


def get_entity(session, entity_id: str) -> Entity | None:
    row = session.get(MemoryEntityRow, entity_id)
    return _entity(row) if row is not None else None


def find_entities_by_name(session, *, user_id: str, scope: str, name: str,
                          statuses: Sequence[str] = (ENTITY_ACTIVE,)) -> list[Entity]:
    """按规范名**精确**找（0..n 个:同名可以是不同实体;近似匹配不属于这一层）。"""
    rows = session.execute(
        select(MemoryEntityRow).where(MemoryEntityRow.user_id == user_id,
                                      MemoryEntityRow.scope == scope,
                                      MemoryEntityRow.normalized == name_key(name),
                                      MemoryEntityRow.status.in_(tuple(statuses)))
        .order_by(MemoryEntityRow.created_at.asc(), MemoryEntityRow.id.asc())
    ).scalars().all()
    return [_entity(r) for r in rows]


def find_entities_by_key(session, *, user_id: str, scope: str, normalized: str,
                         statuses: Sequence[str] = (ENTITY_ACTIVE,)) -> list[Entity]:
    """按已算好的匹配键找（name_key 的输出;别名 / 提及侧复用同一算法,不必重复算）。"""
    rows = session.execute(
        select(MemoryEntityRow).where(MemoryEntityRow.user_id == user_id,
                                      MemoryEntityRow.scope == scope,
                                      MemoryEntityRow.normalized == normalized,
                                      MemoryEntityRow.status.in_(tuple(statuses)))
        .order_by(MemoryEntityRow.created_at.asc(), MemoryEntityRow.id.asc())
    ).scalars().all()
    return [_entity(r) for r in rows]


def update_entity(session, *, entity_id: str, now: datetime, name: str | None = None,
                  kind: str | None = None) -> bool:
    """规范名 / 类型的更正;内容真变了才 revision +1（对账据此发现投影过期）。"""
    row = session.get(MemoryEntityRow, entity_id)
    if row is None:
        return False
    changed = False
    if name is not None and _clip(name) and name_key(name) != row.normalized:
        row.name = _clip(name)
        row.normalized = name_key(row.name)
        changed = True
    if kind is not None and _clip(kind, 24) and _clip(kind, 24) != row.kind:
        row.kind = _clip(kind, 24)
        changed = True
    if not changed:
        return False
    row.revision += 1
    row.updated_at = now
    session.flush()          # 会话 autoflush=False:显式冲刷,同会话里的后续查询能读到
    return True


def set_entity_status(session, *, entity_id: str, status: str, now: datetime) -> bool:
    """状态迁移（merged / deleted / 复活为 active）。状态没变返回 False。"""
    row = session.get(MemoryEntityRow, entity_id)
    if row is None or row.status == status:
        return False
    row.status = status
    row.deleted_at = None if status == ENTITY_ACTIVE else now
    row.revision += 1
    row.updated_at = now
    session.flush()
    return True


def list_entities(session, *, user_id: str, scope: str, status: str | None = ENTITY_ACTIVE,
                  limit: int = 500, after_id: str = "") -> list[Entity]:
    """按用户列出实体（键集分页,id 升序）——「我的记忆」图状态 / 管理命令用。"""
    conds = [MemoryEntityRow.user_id == user_id, MemoryEntityRow.scope == scope]
    if status:
        conds.append(MemoryEntityRow.status == status)
    if after_id:
        conds.append(MemoryEntityRow.id > after_id)
    rows = session.execute(
        select(MemoryEntityRow).where(*conds).order_by(MemoryEntityRow.id.asc()).limit(limit)
    ).scalars().all()
    return [_entity(r) for r in rows]


def count_entities(session, *, user_id: str, scope: str, status: str | None = ENTITY_ACTIVE) -> int:
    conds = [MemoryEntityRow.user_id == user_id, MemoryEntityRow.scope == scope]
    if status:
        conds.append(MemoryEntityRow.status == status)
    return int(session.execute(select(func.count()).select_from(MemoryEntityRow).where(*conds)).scalar_one())


def entities_page(session, *, scope: str, after_id: str = "", limit: int = 500) -> list[Entity]:
    """按作用域整页取实体（跨用户;重建 / 对账用,键集分页）。"""
    conds = [MemoryEntityRow.scope == scope]
    if after_id:
        conds.append(MemoryEntityRow.id > after_id)
    rows = session.execute(
        select(MemoryEntityRow).where(*conds).order_by(MemoryEntityRow.id.asc()).limit(limit)
    ).scalars().all()
    return [_entity(r) for r in rows]


# ---- 别名（确认别名:同一实体的另一种说法）----

def add_alias(session, *, entity_id: str, user_id: str, scope: str, alias: str,
              origin: str, now: datetime) -> bool:
    """登记别名;同一实体内重复是幂等的（唯一键 + SAVEPOINT）。"""
    alias = _clip(alias)
    if not alias:
        return False
    row = MemoryEntityAliasRow(entity_id=entity_id, user_id=user_id, scope=scope, alias=alias,
                               normalized=name_key(alias), origin=origin, created_at=now)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return False
    return True


def list_aliases(session, *, entity_id: str) -> list[EntityAlias]:
    rows = session.execute(
        select(MemoryEntityAliasRow).where(MemoryEntityAliasRow.entity_id == entity_id)
        .order_by(MemoryEntityAliasRow.id.asc())
    ).scalars().all()
    return [_alias(r) for r in rows]


def find_entities_by_alias(session, *, user_id: str, scope: str, alias: str,
                           statuses: Sequence[str] = (ENTITY_ACTIVE,)) -> list[Entity]:
    """按别名找实体（精确键匹配;可能 0..n 个）。"""
    normalized = name_key(alias)
    rows = session.execute(
        select(MemoryEntityRow)
        .join(MemoryEntityAliasRow, MemoryEntityAliasRow.entity_id == MemoryEntityRow.id)
        .where(MemoryEntityAliasRow.user_id == user_id, MemoryEntityAliasRow.scope == scope,
               MemoryEntityAliasRow.normalized == normalized,
               MemoryEntityRow.status.in_(tuple(statuses)))
        .order_by(MemoryEntityRow.created_at.asc(), MemoryEntityRow.id.asc())
    ).scalars().all()
    return list(dict.fromkeys(_entity(r) for r in rows))


def delete_alias(session, *, entity_id: str, alias: str) -> int:
    return int(session.execute(
        delete(MemoryEntityAliasRow).where(MemoryEntityAliasRow.entity_id == entity_id,
                                           MemoryEntityAliasRow.normalized == name_key(alias))
    ).rowcount)


# ---- 提及 + 消歧证据 ----

def add_mention(session, *, user_id: str, scope: str, surface: str,
                entity_id: str | None = None, status: str = "pending",
                memory_id: str | None = None, turn_key: str = "",
                speaker: str | None = None, evidence: dict | None = None,
                generation: int = 0, now: datetime) -> bool:
    """登记一处提及;同一 (记忆, 轮次, 提及原文) 重复是幂等的。

    待定 / 未消歧就是 entity_id=None（status 记 pending / unresolved）—— **绝不强行绑定**。
    重复登记时**不覆盖**已有消歧结论:撞唯一键直接跳过,已 resolved 的行不会被改回 pending。
    """
    surface = _clip(surface)
    if not surface:
        return False
    row = MemoryEntityMentionRow(
        entity_id=entity_id, user_id=user_id, scope=scope, memory_id=memory_id,
        turn_key=(turn_key or "")[:TURN_KEY_LIMIT], surface=surface,
        normalized=name_key(surface), speaker=_clip(speaker or "") or None,
        status=status, evidence=dict(evidence or {}), generation=int(generation), created_at=now,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return False
    return True


def resolve_mention(session, *, mention_id: int, entity_id: str | None, status: str,
                    evidence: dict | None = None) -> bool:
    """给待定提及补消歧结论（维护 / 人工）;evidence 非空时合并进原证据。"""
    row = session.get(MemoryEntityMentionRow, mention_id)
    if row is None:
        return False
    row.entity_id = entity_id
    row.status = status
    if evidence:
        merged = dict(row.evidence or {})
        merged.update(evidence)
        row.evidence = merged
    session.flush()
    return True


def list_pending_mentions(session, *, user_id: str, scope: str, limit: int = 200) -> list[EntityMention]:
    rows = session.execute(
        select(MemoryEntityMentionRow)
        .where(MemoryEntityMentionRow.user_id == user_id, MemoryEntityMentionRow.scope == scope,
               MemoryEntityMentionRow.status == "pending")
        .order_by(MemoryEntityMentionRow.id.asc()).limit(limit)
    ).scalars().all()
    return [_mention(r) for r in rows]


def mentions_for_memory(session, memory_id: str) -> list[EntityMention]:
    rows = session.execute(
        select(MemoryEntityMentionRow).where(MemoryEntityMentionRow.memory_id == memory_id)
        .order_by(MemoryEntityMentionRow.id.asc())
    ).scalars().all()
    return [_mention(r) for r in rows]


# ---- 关系 ----

def _object_key(object_entity_id: str | None, object_text: str) -> str:
    """宾语去重键:宾语是实体 → 实体 id;否则 → 原文的匹配键（与列注释同一口径）。"""
    return object_entity_id or name_key(object_text)


def create_relation(session, *, user_id: str, scope: str, subject_entity_id: str,
                    relation_type: str, object_entity_id: str | None, object_text: str,
                    memory_id: str, generation: int, now: datetime,
                    event_time: datetime | None = None, state: str = "unknown") -> Relation:
    """新建关系（调用方先 find_relation 去重:同一关系重复出现应当**复用本行加来源**）。"""
    object_text = _clip(object_text)
    row = MemoryRelationRow(
        id=new_id(), user_id=user_id, scope=scope, subject_entity_id=subject_entity_id,
        relation_type=_clip(relation_type, 64), object_entity_id=object_entity_id,
        object_text=object_text, object_normalized=_object_key(object_entity_id, object_text),
        memory_id=memory_id, event_time=event_time, state=state, status="active",
        generation=int(generation), revision=1, created_at=now, updated_at=now,
    )
    session.add(row)
    session.flush()
    return _relation(row)


def get_relation(session, relation_id: str) -> Relation | None:
    row = session.get(MemoryRelationRow, relation_id)
    return _relation(row) if row is not None else None


def find_relations(session, *, user_id: str, scope: str, subject_entity_id: str,
                   relation_type: str, object_entity_id: str | None, object_text: str,
                   statuses: Sequence[str] = ("active",)) -> list[Relation]:
    """找同一条关系（主语 + 类型 + 宾语;0..n,理论上同一键只有一条 active,防御性返回列表）。"""
    rows = session.execute(
        select(MemoryRelationRow).where(
            MemoryRelationRow.user_id == user_id, MemoryRelationRow.scope == scope,
            MemoryRelationRow.subject_entity_id == subject_entity_id,
            MemoryRelationRow.relation_type == _clip(relation_type, 64),
            MemoryRelationRow.object_normalized == _object_key(object_entity_id, object_text),
            MemoryRelationRow.status.in_(tuple(statuses)))
        .order_by(MemoryRelationRow.created_at.asc(), MemoryRelationRow.id.asc())
    ).scalars().all()
    return [_relation(r) for r in rows]


def update_relation(session, *, relation_id: str, now: datetime,
                    state: str | None = None, event_time: datetime | None = None,
                    touch_event_time: bool = False) -> bool:
    """维护更新（状态迁移 / 时间更正）;**内容真变了才 revision +1**。"""
    row = session.get(MemoryRelationRow, relation_id)
    if row is None:
        return False
    changed = False
    if state is not None and state != row.state:
        row.state = state
        changed = True
    if touch_event_time and event_time != row.event_time:
        row.event_time = event_time
        changed = True
    if not changed:
        return False
    row.revision += 1
    row.updated_at = now
    session.flush()
    return True


def set_relation_status(session, *, relation_id: str, status: str, now: datetime) -> bool:
    row = session.get(MemoryRelationRow, relation_id)
    if row is None or row.status == status:
        return False
    row.status = status
    row.deleted_at = None if status == "active" else now
    row.revision += 1
    row.updated_at = now
    session.flush()
    return True


def relations_for_entity(session, *, user_id: str, scope: str, entity_id: str,
                         status: str = "active", limit: int = 100) -> list[Relation]:
    """一个实体作为主语 / 宾语参与的关系（图遍历用;limit 有界,高连接度实体不得全量展开）。"""
    rows = session.execute(
        select(MemoryRelationRow).where(
            MemoryRelationRow.user_id == user_id, MemoryRelationRow.scope == scope,
            MemoryRelationRow.status == status,
            (MemoryRelationRow.subject_entity_id == entity_id)
            | (MemoryRelationRow.object_entity_id == entity_id))
        .order_by(MemoryRelationRow.updated_at.desc(), MemoryRelationRow.id.asc())
        .limit(limit)
    ).scalars().all()
    return [_relation(r) for r in rows]


def relations_for_memory(session, memory_id: str,
                         status: str | None = "active") -> list[Relation]:
    conds = [MemoryRelationRow.memory_id == memory_id]
    if status:
        conds.append(MemoryRelationRow.status == status)
    rows = session.execute(
        select(MemoryRelationRow).where(*conds).order_by(MemoryRelationRow.id.asc())
    ).scalars().all()
    return [_relation(r) for r in rows]


def relations_page(session, *, scope: str, after_id: str = "", limit: int = 500) -> list[Relation]:
    conds = [MemoryRelationRow.scope == scope]
    if after_id:
        conds.append(MemoryRelationRow.id > after_id)
    rows = session.execute(
        select(MemoryRelationRow).where(*conds).order_by(MemoryRelationRow.id.asc()).limit(limit)
    ).scalars().all()
    return [_relation(r) for r in rows]


# ---- 事件 ----

def create_event(session, *, user_id: str, scope: str, kind: str, description: str,
                 memory_id: str, generation: int, now: datetime,
                 time_expression: str = "", anchor: datetime | None = None,
                 start_at: datetime | None = None, end_at: datetime | None = None,
                 time_precision: str = "unknown", status: str = "unknown",
                 attributes: dict | None = None) -> Event:
    """新建事件（调用方先 find_duplicate_event:同一记忆里同一次事件的重复提及要合并计数）。"""
    description = _clip(description, 2000)
    row = MemoryEventRow(
        id=new_id(), user_id=user_id, scope=scope, kind=_clip(kind, 24) or "unknown",
        description=description, normalized=content_hash(description),
        time_expression=_clip(time_expression, 200), anchor=anchor, start_at=start_at,
        end_at=end_at, time_precision=time_precision, status=status,
        attributes=dict(attributes or {}), memory_id=memory_id, generation=int(generation),
        revision=1, created_at=now, updated_at=now,
    )
    session.add(row)
    session.flush()
    return _event(row)


def get_event(session, event_id: str) -> Event | None:
    row = session.get(MemoryEventRow, event_id)
    return _event(row) if row is not None else None


def find_duplicate_event(session, *, memory_id: str, description: str) -> Event | None:
    """同一记忆内描述规范化后相同的事件 = 同一次事件的重复提及（去重键:normalized）。"""
    row = session.execute(
        select(MemoryEventRow).where(MemoryEventRow.memory_id == memory_id,
                                     MemoryEventRow.normalized == content_hash(description))
        .order_by(MemoryEventRow.id.asc()).limit(1)
    ).scalars().first()
    return _event(row) if row is not None else None


def update_event(session, *, event_id: str, now: datetime, status: str | None = None,
                 start_at: datetime | None = None, end_at: datetime | None = None,
                 time_precision: str | None = None, attributes: dict | None = None) -> bool:
    """维护更新（状态 / 时间更正 / 属性补充）;内容真变了才 revision +1。"""
    row = session.get(MemoryEventRow, event_id)
    if row is None:
        return False
    changed = False
    if status is not None and status != row.status:
        row.status = status
        changed = True
    if time_precision is not None and time_precision != row.time_precision:
        row.time_precision = time_precision
        changed = True
    if start_at is not None and start_at != row.start_at:
        row.start_at = start_at
        changed = True
    if end_at is not None and end_at != row.end_at:
        row.end_at = end_at
        changed = True
    if attributes:
        merged = dict(row.attributes or {})
        for key, value in attributes.items():
            if merged.get(key) != value:
                merged[key] = value
                changed = True
        if changed:
            row.attributes = merged
    if not changed:
        return False
    row.revision += 1
    row.updated_at = now
    session.flush()
    return True


def events_for_memory(session, memory_id: str, *, include_deleted: bool = False) -> list[Event]:
    conds = [MemoryEventRow.memory_id == memory_id]
    if not include_deleted:
        conds.append(MemoryEventRow.deleted_at.is_(None))
    rows = session.execute(
        select(MemoryEventRow).where(*conds).order_by(MemoryEventRow.id.asc())
    ).scalars().all()
    return [_event(r) for r in rows]


def events_page(session, *, scope: str, after_id: str = "", limit: int = 500) -> list[Event]:
    conds = [MemoryEventRow.scope == scope]
    if after_id:
        conds.append(MemoryEventRow.id > after_id)
    rows = session.execute(
        select(MemoryEventRow).where(*conds).order_by(MemoryEventRow.id.asc()).limit(limit)
    ).scalars().all()
    return [_event(r) for r in rows]


# ---- 事件参与者 ----

def add_participant(session, *, event_id: str, user_id: str, scope: str, role: str,
                    name_text: str, now: datetime, entity_id: str | None = None) -> bool:
    """登记参与者;同一 (事件, 角色, 名称) 重复是幂等的。未消歧时 entity_id=None。"""
    name_text = _clip(name_text)
    if not name_text:
        return False
    row = MemoryEventParticipantRow(
        event_id=event_id, user_id=user_id, scope=scope, role=_clip(role, 64) or "other",
        entity_id=entity_id, name_text=name_text, normalized=name_key(name_text),
        created_at=now,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return False
    return True


def link_participant_entity(session, *, event_id: str, role: str, name_text: str,
                            entity_id: str) -> bool:
    """消歧完成后给参与者补实体引用（按事件 + 角色 + 名称定位那一行）。"""
    row = session.execute(
        select(MemoryEventParticipantRow).where(
            MemoryEventParticipantRow.event_id == event_id,
            MemoryEventParticipantRow.role == (_clip(role, 64) or "other"),
            MemoryEventParticipantRow.normalized == name_key(name_text))
    ).scalars().first()
    if row is None or row.entity_id == entity_id:
        return False
    row.entity_id = entity_id
    session.flush()
    return True


def participants_for_events(session, event_ids: Sequence[str]) -> dict[str, list[EventParticipant]]:
    """批量取参与者（按事件分组;投影 / 重建用,避免 N+1）。"""
    ids = list(dict.fromkeys(event_ids))
    if not ids:
        return {}
    rows = session.execute(
        select(MemoryEventParticipantRow).where(MemoryEventParticipantRow.event_id.in_(ids))
        .order_by(MemoryEventParticipantRow.id.asc())
    ).scalars().all()
    out: dict[str, list[EventParticipant]] = {}
    for row in rows:
        out.setdefault(row.event_id, []).append(_participant(row))
    return out


# ---- 事实 ↔ 来源绑定 ----

def link_fact(session, *, fact_kind: str, fact_id: str, memory_id: str, memory_revision: int,
              user_id: str, scope: str, turn_key: str, generation: int, now: datetime,
              quote: str = "", meta: dict | None = None) -> bool:
    """绑定事实到来源记忆（记住绑定时的**有效版本** revision）;重复绑定是幂等的。"""
    if fact_kind not in FACT_KINDS:
        raise ValueError(f"未知事实类型:{fact_kind!r}")
    row = MemoryFactLinkRow(
        fact_kind=fact_kind, fact_id=fact_id, memory_id=memory_id,
        memory_revision=int(memory_revision), user_id=user_id, scope=scope,
        turn_key=(turn_key or "")[:TURN_KEY_LIMIT], quote=_clip(quote, 2000),
        meta=dict(meta or {}), generation=int(generation), created_at=now,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return False
    return True


def links_for_fact(session, *, fact_kind: str, fact_id: str) -> list[FactLink]:
    rows = session.execute(
        select(MemoryFactLinkRow).where(MemoryFactLinkRow.fact_kind == fact_kind,
                                        MemoryFactLinkRow.fact_id == fact_id)
        .order_by(MemoryFactLinkRow.id.asc())
    ).scalars().all()
    return [_link(r) for r in rows]


def links_for_facts(session, *, fact_kind: str, fact_ids: Sequence[str]) -> dict[str, list[FactLink]]:
    ids = list(dict.fromkeys(fact_ids))
    if not ids:
        return {}
    rows = session.execute(
        select(MemoryFactLinkRow).where(MemoryFactLinkRow.fact_kind == fact_kind,
                                        MemoryFactLinkRow.fact_id.in_(ids))
        .order_by(MemoryFactLinkRow.id.asc())
    ).scalars().all()
    out: dict[str, list[FactLink]] = {}
    for row in rows:
        out.setdefault(row.fact_id, []).append(_link(row))
    return out


def links_for_memory(session, memory_id: str, *, fact_kind: str | None = None) -> list[FactLink]:
    conds = [MemoryFactLinkRow.memory_id == memory_id]
    if fact_kind:
        conds.append(MemoryFactLinkRow.fact_kind == fact_kind)
    rows = session.execute(
        select(MemoryFactLinkRow).where(*conds).order_by(MemoryFactLinkRow.id.asc())
    ).scalars().all()
    return [_link(r) for r in rows]


def links_page(session, *, scope: str, after_id: int = 0, limit: int = 500) -> list[FactLink]:
    """按作用域整页取绑定（重建用,自增 id 键集分页）。"""
    conds = [MemoryFactLinkRow.scope == scope]
    if after_id:
        conds.append(MemoryFactLinkRow.id > after_id)
    rows = session.execute(
        select(MemoryFactLinkRow).where(*conds).order_by(MemoryFactLinkRow.id.asc()).limit(limit)
    ).scalars().all()
    return [_link(r) for r in rows]


def facts_for_memory(session, memory_id: str) -> tuple[list[Relation], list[Event]]:
    """一条记忆上的全部事实（删除 / 维护时判断「还有没有来源支持」的入口）。"""
    return relations_for_memory(session, memory_id), events_for_memory(session, memory_id)


def unlink_memory(session, memory_id: str) -> int:
    """删掉一条记忆的全部事实来源绑定（记忆被删时先解绑,再逐事实判孤儿）。

    返回解绑条数;**不**在这里删事实本身 —— 「移除一个来源不得删除仍有支持的关系」
    是业务层规则（解绑后事实若无其它来源才删除）。
    """
    return int(session.execute(
        delete(MemoryFactLinkRow).where(MemoryFactLinkRow.memory_id == memory_id)
    ).rowcount)


# ---- 行级删除（孤儿事实 / 彻底清除）----

def delete_relation(session, relation_id: str) -> int:
    return int(session.execute(
        delete(MemoryRelationRow).where(MemoryRelationRow.id == relation_id)).rowcount)


def delete_event(session, *, event_id: str) -> int:
    """删事件连参与者（参与者只依附事件,没有独立生命周期）。"""
    session.execute(delete(MemoryEventParticipantRow).where(
        MemoryEventParticipantRow.event_id == event_id))
    return int(session.execute(
        delete(MemoryEventRow).where(MemoryEventRow.id == event_id)).rowcount)


def delete_entity(session, entity_id: str) -> int:
    """硬删实体连别名 / 提及（仅在确认无引用时由清除 / 对账调用）。"""
    session.execute(delete(MemoryEntityAliasRow).where(
        MemoryEntityAliasRow.entity_id == entity_id))
    session.execute(delete(MemoryEntityMentionRow).where(
        MemoryEntityMentionRow.entity_id == entity_id))
    return int(session.execute(
        delete(MemoryEntityRow).where(MemoryEntityRow.id == entity_id)).rowcount)


def delete_graph_for_user(session, user_id: str, *, scope: str) -> dict[str, int]:
    """彻底清除:删掉该用户该作用域的全部图行（七张表）。

    顺序:先删依附行的表（参与者 / 别名 / 提及 / 绑定）,再删事实与实体。
    Neo4j 侧的对应清理**不在**这里（登记 memory_ops,由 Worker 补做）—— 先事实源、后投影。
    """

    def _purge(model) -> int:
        return int(session.execute(
            delete(model).where(model.user_id == user_id, model.scope == scope)).rowcount)

    return {
        "participants": _purge(MemoryEventParticipantRow),
        "aliases": _purge(MemoryEntityAliasRow),
        "mentions": _purge(MemoryEntityMentionRow),
        "fact_links": _purge(MemoryFactLinkRow),
        "relations": _purge(MemoryRelationRow),
        "events": _purge(MemoryEventRow),
        "entities": _purge(MemoryEntityRow),
    }


# ---- 计数（状态 / 对账 / 诊断用）----

_GRAPH_MODELS = {
    "entities": MemoryEntityRow,
    "aliases": MemoryEntityAliasRow,
    "mentions": MemoryEntityMentionRow,
    "relations": MemoryRelationRow,
    "events": MemoryEventRow,
    "participants": MemoryEventParticipantRow,
    "fact_links": MemoryFactLinkRow,
}


def graph_counts(session, *, user_id: str | None = None, scope: str) -> dict[str, int]:
    """按作用域（可再按用户）统计七张表的行数 —— 对账与诊断的「事实源侧数字」。"""
    out: dict[str, int] = {}
    for name, model in _GRAPH_MODELS.items():
        conds = [model.scope == scope]
        if user_id is not None:
            conds.append(model.user_id == user_id)
        out[name] = int(session.execute(
            select(func.count()).select_from(model).where(*conds)).scalar_one())
    return out
