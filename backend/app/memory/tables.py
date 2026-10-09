"""长期记忆的 MySQL 表结构（SQLAlchemy 2.x 声明式）。

建表 SQL 见 backend/migrations/0004_memory_tables.up.sql（四张基础表）与
0005_memory_scope_fencing_index_ops.up.sql（增量列 + memory_sources / memory_ops），
由人手工执行；应用启动**不建表、不改表**（同 app/metering/tables.py、
app/auth/tables.py，见 migrations/README.md）。两边的一致性由
tests/test_memory_migration.py 离线逐项比对（列 / 类型 / 可空 / 主键 / 唯一键 / 索引）。

六张表（技术方案 §数据模型）：
  memory_items      记忆事实（事实源）：按用户隔离、跨会话共享；删除是软删
  memory_history    变更审计（只追加）：ADD / UPDATE / DELETE 各留一行
  memory_jobs       后台提取任务：持久化 + 租约认领 + 认领凭证（fencing）+ 退避重试
  memory_user_state 每用户一行：记忆代次（generation）——「彻底删除」时 +1，
                    所有读改写都要带上它，旧任务据此作废（见 repo.assert_generation）
  memory_sources    记忆 ↔ 来源轮次：一条记忆可以来自多轮对话，不是一个 thread_id 列能装下的
  memory_ops        删除 / 清理的可恢复台账：向量清理失败会重试，不是写行日志就算了

约定：时间一律 UTC、DATETIME(6)；不建外键；NOT NULL 列的默认值在 Python 侧给
（表结构里不写 DEFAULT；0005 的增量列在 SQL 里用 ADD NULL → UPDATE 回填 →
MODIFY NOT NULL 三步走，只是为了给存量行一个值）。Qdrant 只是可重建索引，
不在这里、也不在任何表里存向量。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON, BigInteger, CHAR, DateTime, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 与 app/metering/tables.py 同款的 DATETIME(6)：sa.DateTime(6) 的第一个位置参数是 timezone，
# 精度会被丢掉，MySQL 只建出秒级 DATETIME。MySQL 用方言类型拿 fsp=6，SQLite（仅测试）按文本存。
_DT6 = DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")

# 与迁移脚本的表尾一字不差（SQLite 忽略这些方言选项）。
_TABLE_OPTS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class Base(DeclarativeBase):
    pass


class MemoryItemRow(Base):
    __tablename__ = "memory_items"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # 同用户内去重用的规范化正文摘要（软删行不参与去重，所以**不是**唯一键）。
    content_hash: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)      # active / deleted
    origin: Mapped[str] = mapped_column(String(16), nullable=False)      # llm / user
    # 来源会话线程：列宽对齐 conversations.thread_id（160），手添的记忆为 NULL。
    thread_id: Mapped[str | None] = mapped_column(String(160))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 最近一次写入向量索引时的 embeddings_version；空串 = 未索引（待补）。
    embedding_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    indexed_at: Mapped[datetime | None] = mapped_column(_DT6)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(_DT6)
    # ---- 0005 增量列 ----
    # 作用域：空串 = 正式（聊天产生的记忆），'eval:<run-id>' = 离线评测。
    # 评测与正式数据在同一张表里也必须互不可见（普通 Worker 的补索引 / 清理都按它过滤）。
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 事实类型（提取协议里校验通过的那个 kind）：preference / profile / task；空 = 未标注。
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # 事件发生时间（用户表述里的时间）；未知为 NULL —— 不由系统拿 created_at 顶替。
    event_time: Mapped[datetime | None] = mapped_column(_DT6)
    # 写入时的记忆代次：清理按代次过滤（只删本次清除之前的点），索引负载里也带上它。
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 元数据版本：仅**非正文**变更 +1。正文没变就不重新向量化，只刷新索引里的 payload。
    meta_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 已写入索引的版本：等于 revision / meta_version 才算最新（0 = 从未索引）。
    # 只看 embedding_version 是不够的：同一个模型下「正文已改、索引还是旧的」照样是脏的。
    indexed_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    indexed_meta_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fact_context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        # 列表 / 检索语料都按 (用户, 状态) 取、按 updated_at 排序，这是最主要的访问路径
        Index("ix_memory_items_owner", "user_id", "status", "updated_at"),
        Index("ix_memory_items_hash", "user_id", "content_hash"),
        # 跨用户的补索引扫描按作用域过滤（正式 Worker 不碰评测数据，反之亦然）
        Index("ix_memory_items_scope", "scope", "status", "updated_at"),
        _TABLE_OPTS,
    )


class MemoryHistoryRow(Base):
    __tablename__ = "memory_history"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    memory_id: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    event: Mapped[str] = mapped_column(String(16), nullable=False)       # ADD / UPDATE / DELETE
    old_text: Mapped[str | None] = mapped_column(Text)
    new_text: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)       # llm / user / system
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    thread_id: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    old_context: Mapped[dict | None] = mapped_column(JSON)
    new_context: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_memory_history_item", "memory_id", "id"),
        Index("ix_memory_history_owner", "user_id", "id"),
        _TABLE_OPTS,
    )


class MemoryJobRow(Base):
    __tablename__ = "memory_jobs"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)        # extract
    status: Mapped[str] = mapped_column(String(16), nullable=False)      # 见 repo.JOB_*
    dedupe_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(160))
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 入队时该用户的记忆代次：执行与落库前都要比对，用户「彻底删除」之后就作废
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(96))
    lease_expires_at: Mapped[datetime | None] = mapped_column(_DT6)
    last_error: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(_DT6)
    # ---- 0005 增量列 ----
    # 作用域：与 memory_items.scope 同一口径；认领 / 重放都按它过滤（普通 Worker 不认领评测任务）
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 本任务对应的稳定轮次标识（入队时登记）：来源关联的幂等键，优先于正文哈希
    turn_id: Mapped[str | None] = mapped_column(String(160))
    # 认领凭证（fencing token）：每次认领换一个随机串，收尾 / 续租 / 落库前都要一致。
    # 只比 lease_owner 挡不住「上一个持有者的旧提交」（owner 是重启前的身份，字符串不变）。
    claim_token: Mapped[str] = mapped_column(CHAR(32), nullable=False, default="")
    # 阶段结果（输入摘要 / 协议版本 / 模型口径 / 执行状态）：重试不从头再做付费调用；进终态清内容
    stages: Mapped[dict | None] = mapped_column(JSON)
    # 执行结果摘要（计数与标记，不含正文）：状态展示要能区分 succeeded / 部分失败 / 索引待补
    outcome: Mapped[dict | None] = mapped_column(JSON)
    # 事实提交时间：非空表示已提交 —— 收尾失败后的重试不得重复或修改事实
    committed_at: Mapped[datetime | None] = mapped_column(_DT6)

    __table_args__ = (
        UniqueConstraint("user_id", "kind", "scope", "dedupe_key", name="uk_memory_jobs_dedupe"),
        Index("ix_memory_jobs_claim", "status", "next_run_at"),
        Index("ix_memory_jobs_owner", "user_id", "status"),
        Index("ix_memory_jobs_scope", "scope", "status", "next_run_at"),
        _TABLE_OPTS,
    )


class MemoryUserStateRow(Base):
    """每用户、每作用域一行的代次水位（用户第一次写记忆 / 第一次清除时建立）。

    为什么需要它：清除用户记忆之后，之前入队的提取任务、正在跑的维护决策都可能把
    「刚被删掉的事实」再写回来。代次是这些东西的作废开关 —— 入队时记下当时的代次，
    落库前比对；对不上就整批作废（见 repo.assert_generation）。行本身不含任何正文。
    """

    __tablename__ = "memory_user_state"

    user_id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 最近一次「彻底删除」的时间；只作为运维线索 / 界面展示，不参与任何判断。
    purged_at: Mapped[datetime | None] = mapped_column(_DT6)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    scope: Mapped[str] = mapped_column(String(64), primary_key=True, default="")

    __table_args__ = (_TABLE_OPTS,)


class MemorySourceRow(Base):
    """记忆 ↔ 来源轮次（0005）。

    为什么不是 memory_items.thread_id 一列：一条记忆可以来自多轮对话（同一件事在不同会话里
    被重复确认），单个列只能留下最后一次。thread_id 保留为「首个来源」（界面展示用），
    完整的关联在这里。turn_id 是入队时登记的稳定轮次标识 —— 同样的正文在不同会话里是两个
    来源，不会被正文哈希合并掉；同一轮重复入队则由 (memory_id, turn_id) 唯一键挡住。
    """

    __tablename__ = "memory_sources"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    memory_id: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    conversation_id: Mapped[str | None] = mapped_column(String(160))
    turn_id: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)

    __table_args__ = (
        UniqueConstraint("memory_id", "turn_id", name="uk_memory_sources_turn"),
        Index("ix_memory_sources_owner", "user_id", "conversation_id"),
        Index("ix_memory_sources_memory", "memory_id"),
        _TABLE_OPTS,
    )


class MemoryOpRow(Base):
    """删除 / 清理的可恢复台账（0005）：先在事实事务里登记，再在事务外执行。

    没有这张表时，删除一条记忆之后的向量清理失败只会写一行日志 —— 没人会去看，也没有重试；
    下次重建索引只扫「有效记忆」，已删的行永远等不到对账。purge_user 的 generation 是过滤
    依据：只删小于当前代次的点，清除期间用户新写的记忆（新代次）不受影响。
    """

    __tablename__ = "memory_ops"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(24), nullable=False)     # delete_vector / delete_old_versions / purge_user
    memory_id: Mapped[str | None] = mapped_column(CHAR(32))
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), nullable=False)   # 同 memory_jobs 的状态口径
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(96))
    claim_token: Mapped[str] = mapped_column(CHAR(32), nullable=False, default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(_DT6)
    last_error: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(_DT6)

    __table_args__ = (
        Index("ix_memory_ops_claim", "scope", "status", "next_run_at"),
        Index("ix_memory_ops_owner", "user_id", "status"),
        _TABLE_OPTS,
    )
