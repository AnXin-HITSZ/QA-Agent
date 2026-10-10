"""记忆图的 MySQL 表结构（SQLAlchemy 2.x 声明式;事实源）。

七张表（技术方案 §数据模型）:
  memory_entities            实体:稳定 ID + 规范名;同名 ≠ 同一实体（**没有**名字唯一键）
  memory_entity_aliases      确认别名:同一实体的其它说法（唯一键只约束「实体内不重复」）
  memory_entity_mentions     提及 + 消歧证据:待定 / 未消歧时为 NULL 实体,绝不强行绑定
  memory_relations           关系:主语 → 类型 → 宾语（实体或原文短语）;多来源见 fact_links
  memory_events              事件:类型 / 描述 / 原始时间表述 / 锚点 / 可确定范围 + 精度 /
                             计划-已发生-已取消状态 / 有依据的属性 —— 不是三元组
  memory_event_participants  事件参与者 + 角色（实体或原文名称;未消歧也保留）
  memory_fact_links          事实 ↔ 来源绑定:事实绑定原始记忆的**有效版本**（revision）、
                             轮次与原文摘录;一条记忆可映射多条事实,一条事实可有多个来源

建表 SQL 见 backend/migrations/0009_memory_graph_tables.up.sql,由人手工执行;
应用启动**不建表、不改表**（同 app/memory/tables.py、app/metering/tables.py）。
两边的一致性由 tests/test_memory_graph_migration.py 离线逐项比对（列 / 类型 / 可空 /
主键 / 唯一键 / 索引）。

约定（与 0004/0005 完全相同）:时间一律 UTC、DATETIME(6);不建外键;NOT NULL 列的
默认值在 Python 侧给（表结构里不写 DEFAULT）;Neo4j 里放的只是这些行的**可重建投影**,
任何图数据都能从这里重建 —— 反过来不行。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON, BigInteger, CHAR, DateTime, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 与 app/memory/tables.py 同款 DATETIME(6)（理由见那里的注释,不再重复）。
_DT6 = DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")

# 与迁移脚本的表尾一字不差。
_TABLE_OPTS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class Base(DeclarativeBase):
    pass


class MemoryEntityRow(Base):
    """一个实体（人 / 地点 / 机构 / 物 / 主题…）;kind 可扩展,'unknown' 是合法值。

    为什么 (user_id, scope, normalized) 上**没有**唯一键（与 content_hash 的教训同理）:
    同名 ≠ 同一实体。两个「小明」可能是两个人;已删除（合并 / 清除）的实体行也要留在
    表里做审计。重名合并是**消歧决策**（见 memory_entity_mentions 的证据），不是唯一键
    能代替的 —— 唯一键会让「后来的同名实体」无声无息写不进来或撞上已删行。
    """

    __tablename__ = "memory_entities"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 实体类型:person / place / org / product / animal / topic / unknown…（可扩展,未知可表示）
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    name: Mapped[str] = mapped_column(Text, nullable=False)              # 规范名（原文语言）
    # 规范化摘要（NFKC + casefold;同一算法见 models.normalize_text）—— 精确匹配的检索键,
    # 不是唯一键（理由见类注释）。
    normalized: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active/merged/deleted
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)   # 与 memory_items 同口径
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)     # 图元素版本(维护更新 +1)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(_DT6)

    __table_args__ = (
        Index("ix_memory_entities_owner", "user_id", "scope", "status", "updated_at"),
        Index("ix_memory_entities_name", "user_id", "scope", "normalized"),
        _TABLE_OPTS,
    )


class MemoryEntityAliasRow(Base):
    """确认别名:同一实体的另一种说法（唯一键只约束「同一实体内不重复」）。"""

    __tablename__ = "memory_entity_aliases"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    entity_id: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="llm")   # llm / user
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)

    __table_args__ = (
        UniqueConstraint("entity_id", "normalized", name="uk_memory_entity_aliases"),
        Index("ix_memory_entity_aliases_lookup", "user_id", "scope", "normalized"),
        _TABLE_OPTS,
    )


class MemoryEntityMentionRow(Base):
    """一处提及 + 消歧证据;待定 / 未消歧时 entity_id 为 NULL —— **绝不强行绑定**。

    同名 ≠ 同一实体:候选模糊时保留待定（status=pending）,证据存在 evidence 里
    （匹配到的别名 / 类型 / 上下文摘要）,由后续维护或人工裁决;绝不因为「长得像」
    或「话题接近」就合并。
    """

    __tablename__ = "memory_entity_mentions"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    entity_id: Mapped[str | None] = mapped_column(CHAR(32))
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    memory_id: Mapped[str | None] = mapped_column(CHAR(32))              # 提及出现的记忆（可为空）
    # 来源轮次:'' = 无轮次来源（用户手添记忆 / 旧数据）;与 memory_fact_links 同一收敛写法
    turn_key: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    surface: Mapped[str] = mapped_column(Text, nullable=False)           # 提及的原文形态
    normalized: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(160))             # 说话人（适配层提供;无则 NULL）
    status: Mapped[str] = mapped_column(String(16), nullable=False)      # resolved / pending / unresolved
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)

    __table_args__ = (
        # 幂等:同一记忆同一轮次的同一提及只记一次（memory_id 为 NULL 时由 MySQL 语义容忍重复,
        # 去重退化为查询侧尽力而为 —— 冷路径,可接受）。
        UniqueConstraint("memory_id", "turn_key", "normalized", name="uk_memory_entity_mentions"),
        Index("ix_memory_entity_mentions_owner", "user_id", "scope", "status"),
        Index("ix_memory_entity_mentions_entity", "entity_id"),
        _TABLE_OPTS,
    )


class MemoryRelationRow(Base):
    """关系:主语实体 → 类型 → 宾语（实体或原文短语）;方向有意义,不得反转。

    多来源不入本表:完整来源在 memory_fact_links（一条关系可有多个来源;移除一个来源
    不得删除仍有支持的关系）。memory_id 只是**首个来源**（界面展示 / 兼容口径）。
    """

    __tablename__ = "memory_relations"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    subject_entity_id: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    # 通用可验证的关系类型（related_to / works_at / gift_from / has_pet…）:字符串,可扩展,
    # 不把枚举写死在这里 —— 但必须由提取协议校验后才入库。
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_entity_id: Mapped[str | None] = mapped_column(CHAR(32))
    object_text: Mapped[str] = mapped_column(Text, nullable=False, default="")   # 宾语非实体时的原文;是实体时空串
    # 宾语去重键:宾语是实体时为其 id（与 object_entity_id 一致的口径,便于统一比较）,
    # 否则为 object_text 的规范化摘要 —— TEXT 不能做索引/唯一,去重查询靠这一列。
    object_normalized: Mapped[str] = mapped_column(CHAR(32), nullable=False, default="")
    memory_id: Mapped[str] = mapped_column(CHAR(32), nullable=False)            # 首个来源记忆
    event_time: Mapped[datetime | None] = mapped_column(_DT6)                   # 关系发生 / 成立时间;未知 NULL
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")  # 与事实 state 同口径
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active / deleted
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(_DT6)

    __table_args__ = (
        Index("ix_memory_relations_subject", "user_id", "scope", "subject_entity_id", "status"),
        Index("ix_memory_relations_object", "user_id", "scope", "object_entity_id", "status"),
        Index("ix_memory_relations_memory", "memory_id"),
        # 同一关系(主语/类型/宾语)重复出现时复用本行加来源 —— 去重查询的键
        Index("ix_memory_relations_dedupe", "user_id", "scope", "subject_entity_id",
              "relation_type", "object_normalized"),
        _TABLE_OPTS,
    )


class MemoryEventRow(Base):
    """事件:不是三元组 —— 描述 / 原始时间表述 / 锚点 / 可确定范围 + 精度 / 状态 / 有依据的属性。

    时间纪律（与 temporal.py 同一口径）:time_expression 原样摘录;start_at/end_at 只在
    **有可信锚点**时按可确定精度计算（仅年就只到年,用 start/end 圈出范围）;绝不虚构
    精度（不知道哪一天就不编哪一天）。status 区分 planned / ongoing / completed /
    cancelled —— 「打算去」与「去过了」在计数与回答里不是一回事。
    """

    __tablename__ = "memory_events"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")  # 事件类型(可扩展)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # 同一记忆内的事件去重键（规范化描述摘要）:重复提及同一次事件要合并计数,不是两条事件。
    normalized: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    time_expression: Mapped[str] = mapped_column(String(200), nullable=False, default="")  # 原始时间表述
    anchor: Mapped[datetime | None] = mapped_column(_DT6)          # 相对时间的锚点(来源记录时间)
    start_at: Mapped[datetime | None] = mapped_column(_DT6)        # 可确定的范围(按实际精度)
    end_at: Mapped[datetime | None] = mapped_column(_DT6)
    time_precision: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")  # planned/ongoing/completed/cancelled/unknown
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)   # 有依据的属性(不得虚构)
    memory_id: Mapped[str] = mapped_column(CHAR(32), nullable=False)               # 首个来源记忆
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(_DT6)

    __table_args__ = (
        Index("ix_memory_events_owner", "user_id", "scope", "status", "start_at"),
        Index("ix_memory_events_memory", "memory_id"),
        Index("ix_memory_events_dedupe", "memory_id", "normalized"),
        _TABLE_OPTS,
    )


class MemoryEventParticipantRow(Base):
    """事件参与者 + 角色;未消歧时 entity_id 为 NULL、名称原文保留在 name_text。"""

    __tablename__ = "memory_event_participants"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 角色:subject / participant / companion / organizer / recipient / giver…（通用,可扩展）
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(CHAR(32))
    name_text: Mapped[str] = mapped_column(Text, nullable=False)         # 参与者的原文名称 / 描述
    normalized: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)

    __table_args__ = (
        UniqueConstraint("event_id", "role", "normalized", name="uk_memory_event_participants"),
        Index("ix_memory_event_participants_owner", "user_id", "scope", "entity_id"),
        _TABLE_OPTS,
    )


class MemoryFactLinkRow(Base):
    """事实 ↔ 来源绑定:绑定「原始记忆的**有效版本**」,不是只记一个 memory_id。

    一条记忆可以映射多条事实（一次对话说了好几件事）,一条事实可以有多个来源
    （同一件事在不同轮次被重复确认）—— 所以是独立表。memory_revision 记下绑定时的
    记忆版本:记忆正文更新后可检出「绑定引用的版本已过期」（对账 / 维护的依据）。
    quote 保留原始表述;meta 保存不确定性（如 unresolved 指代）。
    """

    __tablename__ = "memory_fact_links"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    fact_kind: Mapped[str] = mapped_column(String(16), nullable=False)   # relation / event
    fact_id: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    memory_id: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    memory_revision: Mapped[int] = mapped_column(Integer, nullable=False)  # 绑定时的有效记忆版本
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 来源轮次:'' = 无轮次来源（用户手添记忆 / 旧数据）
    turn_key: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    quote: Mapped[str] = mapped_column(Text, nullable=False, default="")   # 原始表述摘录
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # 不确定性 / 解析证据
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)

    __table_args__ = (
        # 幂等:同一事实 × 同一记忆 × 同一轮次只记一条（重复确认由不同轮次体现）
        UniqueConstraint("fact_kind", "fact_id", "memory_id", "turn_key",
                         name="uk_memory_fact_links"),
        Index("ix_memory_fact_links_memory", "memory_id", "fact_kind"),
        Index("ix_memory_fact_links_owner", "user_id", "scope", "fact_kind"),
        _TABLE_OPTS,
    )
