"""MySQL 表结构(SQLAlchemy 2.x 声明式;建表 SQL 见 backend/migrations/*.up.sql,手工执行)。

四张表(技术方案 §6):
  call_events        调用流水:一次真实外部请求一行(主键 = 事件 id,天然幂等)
  call_event_items   批量请求 → 文件 / 页面 的估算分摊(一次请求覆盖多文件时用)
  cache_events       缓存命中 / 未命中统计(与费用流水分表,不重复计费)
  price_config       价格配置:按 服务 / 供应商 / 模型或 OCR Type / 计费单位 配置,
                     每个事件在发生时刻取当时生效的一行做**不可变快照**

约定:
- 时间一律 UTC,DATETIME(6)(微秒精度);金额一律 DECIMAL,绝不用 float;
- 文本字段按「实际会被查询 / 排序 / 聚合的」建索引,其余留在 JSON 快照里;
- 唯一约束 / 主键保证补写重放不会产生重复行(INSERT ... ON DUPLICATE KEY 幂等)。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON, BigInteger, CHAR, DateTime, Index, Integer, Numeric, SmallInteger, String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.metering.model import utcnow


class Base(DeclarativeBase):
    pass


# 时间列一律 DATETIME(6)(微秒)。注意不能写 sa.DateTime(6):那个位置参数是 timezone,
# 精度会被丢掉、MySQL 只建出秒级的 DATETIME —— 而同一逻辑调用的多次尝试常落在同一秒内,
# 排序与 retry_of 关联都靠微秒。MySQL 用方言类型拿 fsp=6,SQLite(仅测试)按文本存。
_DT6 = DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")

# 表选项与迁移脚本一字不差(迁移在 __table_args__ 末尾追加字典),这样「模型 ↔ 迁移」
# 可以用离线 DDL 逐字对比(tests/test_metering_migration.py);SQLite 忽略这些方言选项。
_TABLE_OPTS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def _utcnow() -> datetime:
    return utcnow().replace(tzinfo=None)   # 库里统一存 naive UTC


class CallEventRow(Base):
    __tablename__ = "call_events"

    event_id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False, default=_utcnow)
    service: Mapped[str] = mapped_column(String(16), nullable=False)          # embedding / ocr
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)          # document_index / query
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(String(64), nullable=False, default="")   # 模型名 / OCR Type
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    call_group: Mapped[str | None] = mapped_column(CHAR(32))
    attempt_no: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    retry_of: Mapped[str | None] = mapped_column(CHAR(32))
    http_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False)           # success / failure
    error_class: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    # 用量:quantity 允许 NULL(拿不到就空着,不用 0 冒充)
    usage_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    usage_unit: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    usage_source: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    usage_note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    billing_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    billing_unit: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    billing_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    billing_note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, default="")
    cost_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    cost_note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    price_id: Mapped[int | None] = mapped_column(BigInteger)
    price_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    price_snapshot: Mapped[dict | None] = mapped_column(JSON)
    job_id: Mapped[str | None] = mapped_column(String(64))
    # 文档身份(app/rag/documents.py 登记)是带连字符的 UUID 字符串,36 位 —— 不是本模块
    # event_id 那种 32 位 hex。列宽按它来:短一位,MySQL 严格模式下整批 INSERT 直接
    # 1406 Data too long(SQLite 不校验 CHAR 长度,本地全绿也发现不了),0002 迁移为此放宽。
    document_id: Mapped[str | None] = mapped_column(CHAR(36))
    oss_key: Mapped[str | None] = mapped_column(String(512))
    page_no: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        # 列表 / 概览都按时间倒序 + 服务过滤,这是最主要的访问路径
        Index("ix_call_events_time_service", "occurred_at", "service"),
        Index("ix_call_events_job", "job_id"),
        Index("ix_call_events_document", "document_id"),
        Index("ix_call_events_group", "call_group"),
        _TABLE_OPTS,
    )


class CallEventItemRow(Base):
    __tablename__ = "call_event_items"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    # NULL 字段的占位(norm)。最长形态是 document_id + ":" + oss_key(36 + 1 + 512 = 549),
    # 按 oss_key 列的上限取整,别按「常见路径」估 —— 长路径会撞 1406(0002 迁移放宽到 549)。
    item_key: Mapped[str] = mapped_column(String(549), nullable=False)
    document_id: Mapped[str | None] = mapped_column(CHAR(36))   # 文档身份:36 位(见 CallEventRow)
    oss_key: Mapped[str | None] = mapped_column(String(512))
    page_no: Mapped[int | None] = mapped_column(Integer)
    text_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    allocated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    allocation_note: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("event_id", "item_key", name="uq_call_event_items_event_item"),
        Index("ix_call_event_items_document", "document_id"),
        _TABLE_OPTS,
    )


class CacheEventRow(Base):
    __tablename__ = "cache_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False, default=_utcnow)
    layer: Mapped[str] = mapped_column(String(16), nullable=False)      # ocr_raw / ocr_text / embedding
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)       # page / text
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    miss_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shared_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    job_id: Mapped[str | None] = mapped_column(String(64))
    document_id: Mapped[str | None] = mapped_column(CHAR(36))   # 文档身份:36 位(见 CallEventRow)
    oss_key: Mapped[str | None] = mapped_column(String(512))
    page_no: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    __table_args__ = (
        # 显式命名:库里的键由手工 SQL 建(unique=True 会生成无名约束,两边的名字对不上)
        UniqueConstraint("event_id", name="uk_cache_events_event_id"),
        Index("ix_cache_events_time_layer", "occurred_at", "layer"),
        Index("ix_cache_events_job", "job_id"),
        _TABLE_OPTS,
    )


class PriceConfigRow(Base):
    __tablename__ = "price_config"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(16), nullable=False)        # embedding / ocr
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(String(64), nullable=False, default="")  # 模型 / OCR Type;空 = 通用
    unit: Mapped[str] = mapped_column(String(16), nullable=False)           # 1k_tokens / request / page
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(_DT6, nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(_DT6, nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("service", "provider", "target", "unit", "currency", "effective_from",
                         name="uq_price_config_rule"),
        _TABLE_OPTS,
    )
