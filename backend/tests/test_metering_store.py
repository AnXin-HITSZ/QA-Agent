"""仓库层:真 MysqlStore 跑在 SQLite 上,验入库行与 NOT NULL 列的对应。

为什么单开一个文件:默认的内存仓库是 dict,没有列约束,盖不住「入库行给 NULL」这类 bug。
生产实测过一次 —— 未命中价目的记录 price_version 是空串,JSON 形态把它写成 null,
入库直接撞 `price_version VARCHAR(64) NOT NULL`(1048);记录转入补写目录后每次重放都
撞同一个错,积压永不消失(现象:调用与费用页「待补写」长期不清、「已落盘」随 worker 跳)。

SQLite 只验证「模型这一侧」的约束语义,**不等于 MySQL 已验证**(见 test_metering_mysql.py)。
建表用模型元数据 create_all —— 仅测试可以这么干,app/ 里禁止自动 DDL(有专门用例看守)。

为什么真正把关的是行级用例:SQLite 分支走 SQLAlchemy 的 ORM 批量插入,None 会被 Python 侧
列默认值填上,把这个错盖住;MySQL 分支是内联多值 INSERT(不填默认值),生产就是在那里炸的。
所以 `test_event_row_keeps_not_null_columns_non_null` 盯住「入库行不许给 NULL」,
端到端用例只保证链路整体行为(能入库、不转补写)。
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.metering import build_call, record_call
from app.metering.db import session_scope
from app.metering.model import COST_UNKNOWN, SERVICE_EMBEDDING, STATUS_SUCCESS, USAGE_SOURCE_VENDOR
from app.metering.store import CallFilter, MysqlStore
from app.metering.tables import CallEventRow


def _unpriced_call():
    """一条「未命中价目」的调用(生产上卡死补写的那类记录:price_version 为空串)。"""
    return build_call(service=SERVICE_EMBEDDING, provider="dashscope",
                      target="qwen3.7-text-embedding-flash", endpoint="dashscope.aliyuncs.com",
                      duration_ms=100, status=STATUS_SUCCESS,
                      usage_quantity=Decimal(1000), usage_unit="token",
                      usage_source=USAGE_SOURCE_VENDOR, price_unit="1k_tokens")


@pytest.fixture
def sqlite_env(monkeypatch, tmp_path):
    """真 MysqlStore + 临时 SQLite 文件 + 不入队的写入器;补写目录也在 tmp 下。"""
    from app.config import get_settings
    from app.metering import db, install_store, install_writer
    from app.metering.tables import Base
    from app.metering.writer import MeteringWriter

    class _Writer(MeteringWriter):
        def _ensure_thread(self) -> None:
            return

        def start(self) -> None:      # 后台线程不启动:队列由测试自己 flush
            return

    s = get_settings()
    monkeypatch.setattr(s, "metering_enabled", True)
    monkeypatch.setattr(s, "metering_mysql_url",
                        f"sqlite+pysqlite:///{(tmp_path / 'metering.db').as_posix()}")
    monkeypatch.setattr(s, "metering_pending_dir", str(tmp_path / "pending"))

    db.dispose()                                  # 别让别的用例的引擎跨到这里
    Base.metadata.create_all(db.get_engine())     # 仅测试;app/ 里禁止自动 DDL
    store = MysqlStore()
    install_store(store)
    writer = _Writer()
    install_writer(writer)
    yield SimpleNamespace(store=store, writer=writer, settings=s, pending=tmp_path / "pending")
    install_writer(None)
    install_store(None)
    db.dispose()


def test_unpriced_event_flushes_into_real_store_without_spilling(sqlite_env):
    """整条链路(队列 → 真 MysqlStore → SQLite):未命中价目的记录能入库、不转补写。"""
    record_call(_unpriced_call())
    result = sqlite_env.writer.flush()

    assert result["written"] == 1
    assert sqlite_env.writer.status()["spilled"] == 0
    assert not list(sqlite_env.pending.glob("*.json"))            # 补写目录保持空

    rows = sqlite_env.store.list_calls(CallFilter(), offset=0, limit=10)["items"]
    assert len(rows) == 1 and rows[0]["cost_status"] == COST_UNKNOWN
    with session_scope() as session:                              # 列存空串,不是 NULL
        assert session.execute(select(CallEventRow.price_version)).scalar_one() == ""


def test_event_row_keeps_not_null_columns_non_null(metering_env):
    """入库行对每个 NOT NULL 列都必须有值:as_payload 是 JSON 形态(空值写成 null),不能直接进库。"""
    row = MysqlStore._event_row(_unpriced_call())
    assert row["price_version"] == ""
    for col in CallEventRow.__table__.columns:
        if col.name in row and not col.nullable:
            assert row[col.name] is not None, f"{col.name} 是 NOT NULL 列,入库行不能为 None"
