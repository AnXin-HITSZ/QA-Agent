"""认证 / 用户管理 / 会话目录的 MySQL 表结构（SQLAlchemy 2.x 声明式）。

建表 SQL 见 backend/migrations/0003_auth_and_conversations.up.sql，由人手工执行；
应用启动**不建表、不改表**（同 app/metering/tables.py，见 migrations/README.md）。
两边的一致性由 tests/test_auth_migration.py 离线逐项比对（列 / 类型 / 可空 / 主键 / 唯一键 / 索引）。

六张表（技术方案 §7）：
  users                用户：身份、角色、状态机、auth_version（令牌版本）
  auth_sessions        登录会话（一行 = 一次登录）；撤销按行标记，不删行
  auth_refresh_tokens  refresh token 轮换链；被消费的旧令牌被再次使用即可判定重放
  email_tokens         邮箱令牌（验证邮箱 / 重置密码，purpose 隔离，一次性）
  conversations        会话目录（归属 + 内部线程 id + 删除状态机）
  auth_audit           认证与用户管理审计（只追加）

约定：时间一律 UTC、DATETIME(6)；令牌只存 SHA-256 摘要；密码只存 Argon2id 编码串；
不建外键；NOT NULL 列的默认值在 Python 侧给（表结构里不写 DEFAULT）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, CHAR, DateTime, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 与 app/metering/tables.py 同款的 DATETIME(6)：sa.DateTime(6) 的第一个位置参数是 timezone，
# 精度会被丢掉，MySQL 只建出秒级 DATETIME。MySQL 用方言类型拿 fsp=6，SQLite（仅测试）按文本存。
_DT6 = DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")

# 与迁移脚本的表尾一字不差（SQLite 忽略这些方言选项）。
_TABLE_OPTS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}

# 邮箱列的排序规则：规范化后整体小写，唯一键与查询都必须按「精确字符串相等」判定。
# 不跟随库默认的 utf8mb4_0900_ai_ci —— 它不区分大小写也不区分重音，会把 é / e 合并。
EMAIL_COLLATION = "utf8mb4_bin"


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(254, collation=EMAIL_COLLATION), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)       # user / admin
    status: Mapped[str] = mapped_column(String(24), nullable=False)     # 见 STATUS_*
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(_DT6)
    reviewed_at: Mapped[datetime | None] = mapped_column(_DT6)
    reviewed_by: Mapped[str | None] = mapped_column(CHAR(36))
    review_note: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(_DT6)

    __table_args__ = (
        UniqueConstraint("email", name="uk_users_email"),
        Index("ix_users_status", "status", "created_at"),
        Index("ix_users_role", "role"),
        _TABLE_OPTS,
    )


class AuthSessionRow(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    user_agent: Mapped[str] = mapped_column(String(255), nullable=False)
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(_DT6)
    revoked_reason: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("ix_auth_sessions_user", "user_id", "revoked_at"),
        Index("ix_auth_sessions_expires", "expires_at"),
        _TABLE_OPTS,
    )


class AuthRefreshTokenRow(Base):
    __tablename__ = "auth_refresh_tokens"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(_DT6)

    __table_args__ = (
        UniqueConstraint("token_hash", name="uk_auth_refresh_tokens_hash"),
        Index("ix_auth_refresh_tokens_session", "session_id", "consumed_at"),
        _TABLE_OPTS,
    )


class EmailTokenRow(Base):
    __tablename__ = "email_tokens"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)    # verify_email / reset_password
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(_DT6)
    ip: Mapped[str] = mapped_column(String(45), nullable=False)

    __table_args__ = (
        UniqueConstraint("token_hash", name="uk_email_tokens_hash"),
        Index("ix_email_tokens_user", "user_id", "purpose", "consumed_at"),
        _TABLE_OPTS,
    )


class ConversationRow(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)     # active / deleting / deleted
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(_DT6)

    __table_args__ = (
        UniqueConstraint("thread_id", name="uk_conversations_thread"),
        Index("ix_conversations_owner", "user_id", "status", "updated_at"),
        _TABLE_OPTS,
    )


class AuthAuditRow(Base):
    __tablename__ = "auth_audit"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)     # ok / denied / failed
    actor_user_id: Mapped[str | None] = mapped_column(CHAR(36))
    target_user_id: Mapped[str | None] = mapped_column(CHAR(36))
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    note: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        Index("ix_auth_audit_time", "occurred_at"),
        Index("ix_auth_audit_action", "action", "occurred_at"),
        Index("ix_auth_audit_target", "target_user_id", "occurred_at"),
        _TABLE_OPTS,
    )
