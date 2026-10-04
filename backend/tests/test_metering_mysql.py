"""MySQL 专属集成测试:迁移往返 / 真表结构 / 唯一约束 / Decimal / 微秒 / 并发幂等写 / 补写。

**默认全部跳过**:没设 METERING_TEST_MYSQL_URL 时一行都不连。SQLite 上的绿灯不能冒充
「MySQL 已验证」(技术方案 §10),所以本文件在没有 MySQL 的环境里只负责说清「没验」,
不假装通过。设了也必须指向可弃的测试库:库名里不含 test / scratch / tmp / dev 直接判失败,
免得有人把 DSN 指到 qa_agent_prod 上让测试建表 / 删表。

跑法(库先由 DBA / 自己建好,账号只要有建表权限即可,不需要 root):
    set METERING_TEST_MYSQL_URL=mysql+pymysql://user:pw@host:3306/qa_agent_dev?charset=utf8mb4
    python -m pytest tests/test_metering_mysql.py -q

库里的 metering 表由 migrations/ 下的 up.sql 在开始时逐条执行建好、结束时 down.sql 删掉
(生产上就是 `mysql <db> < 0001....up.sql`,见 migrations/README.md);若进去发现表已存在,
直接判失败(宁可让你换一个空库,也不在别人正在用的库上练迁移)。这张表是「本文件最后一个
用例」之外的共享状态,所以每个写用例前都清一次数据,行数断言才站得住。
"""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.pool import NullPool

from app.metering import CallItem
from app.metering.model import COST_ESTIMATED
from app.metering.store import CallFilter
from tests import meterkit as mk
from tests import migrationkit as mig

TABLES = ("call_events", "call_event_items", "cache_events", "price_config")
ENV = "METERING_TEST_MYSQL_URL"
UTC = timezone.utc

# 库名必须自证「可弃」:qa_agent_dev / qa_agent_test / xxx_scratch 都行,qa_agent_prod 不行。
_SCRATCH_NAME = re.compile(r"(^|[_-])(test|testing|scratch|tmp|dev)([_-]|$)")


def _dsn() -> str:
    raw = (os.environ.get(ENV) or "").strip()
    if not raw:
        pytest.skip(f"未设置 {ENV}:MySQL 集成项未验证(SQLite 通过不等于 MySQL 通过)")
    database = (make_url(raw).database or "")
    if not _SCRATCH_NAME.search(database.lower()):
        pytest.fail(f"{ENV} 指向的库 {database!r} 不像测试库:拒绝在它上面建表 / 删表"
                    "(请用 qa_agent_dev,或 *_test / *_scratch 结尾的库)", pytrace=False)
    return raw


def _apply(dsn: str, direction: str) -> None:
    """把 migrations/ 下该方向的 SQL 全部执行一遍(等价于人手逐条 `mysql <db> < file`)。

    回滚按版本倒序执行(与上线相反);出错直接抛,由用例 / 夹具暴露,不吞。
    """
    items = mig.migrations(direction)
    if direction == "down":
        items = list(reversed(items))
    engine = _engine(dsn)
    try:
        for item in items:
            with engine.begin() as conn:
                for stmt in mig.statements(mig.text(item.path)):
                    conn.exec_driver_sql(stmt)
    finally:
        engine.dispose()


def _engine(dsn: str):
    """探测 / 断言用的引擎:NullPool,用完即断,不给远端测试库留常驻连接。"""
    return create_engine(dsn, poolclass=NullPool)


def _query(engine, sql: str, **params) -> list[dict]:
    with engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(text(sql), params)]


def _columns(engine, table: str) -> dict[str, dict]:
    """information_schema 里的真实列信息(建表语句写在迁移里,这里读的是库认定的事实)。"""
    rows = _query(engine, """
        SELECT COLUMN_NAME, IS_NULLABLE, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
               NUMERIC_PRECISION, NUMERIC_SCALE, DATETIME_PRECISION
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table
    """, table=table)
    return {r["COLUMN_NAME"]: r for r in rows}


def _clear(engine) -> None:
    with engine.begin() as conn:
        for table in ("call_event_items", "call_events", "cache_events", "price_config"):
            conn.execute(text(f"DELETE FROM {table}"))


def _count(engine, table: str) -> int:
    return _query(engine, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def _priced(event, amount: str, currency: str = "CNY"):
    """金额直接写死:本文件关心的是「Decimal 能不能原样进出一趟 MySQL」,不是估价逻辑。"""
    return event.with_(cost_amount=Decimal(amount), currency=currency, cost_status=COST_ESTIMATED,
                       cost_note="测试:按配置单价 × 用量估算")


# ---- 护栏本身也要有测试:指到生产库名必须判失败,而不是悄悄跳过或照跑 ----


def test_guard_refuses_a_production_looking_database(monkeypatch):
    monkeypatch.setenv(ENV, "mysql+pymysql://u:p@db:3306/qa_agent_prod")
    with pytest.raises(pytest.fail.Exception) as exc:      # pytest.fail 抛的 Failed
        _dsn()
    assert "qa_agent_prod" in str(exc.value)


def test_guard_accepts_a_scratch_database(monkeypatch):
    monkeypatch.setenv(ENV, "mysql+pymysql://u:p@db:3306/qa_agent_dev")
    assert make_url(_dsn()).database == "qa_agent_dev"


def test_guard_skips_when_unset(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        _dsn()


# ---- 夹具:库 → 迁移 → 干净的表 ----


@pytest.fixture(scope="module")
def dsn() -> str:
    return _dsn()


@pytest.fixture(scope="module")
def mysql(dsn):
    """空库 → 执行全部 up.sql → 用例;跑完执行全部 down.sql 回空。"""
    engine = _engine(dsn)
    clash = sorted(set(inspect(engine).get_table_names()) & set(TABLES))
    engine.dispose()
    if clash:
        pytest.fail(f"测试库 {make_url(dsn).database} 里已经有 {clash}:"
                    "请换一个空的测试库(或先手工清掉这些表),别拿正在用的库练迁移", pytrace=False)

    _apply(dsn, "up")

    engine = _engine(dsn)
    yield engine
    engine.dispose()
    _apply(dsn, "down")          # 清场失败会直接抛,同样让用例红


@pytest.fixture
def store(dsn, mysql, monkeypatch, tmp_path):
    """把设置指向测试库,用**真的 MysqlStore**(不是内存实现)。"""
    from app.config import get_settings
    from app.metering import db, install_store, install_writer
    from app.metering.store import MysqlStore

    s = get_settings()
    monkeypatch.setattr(s, "metering_enabled", True)
    monkeypatch.setattr(s, "metering_mysql_url", dsn)
    monkeypatch.setattr(s, "metering_pending_dir", str(tmp_path / "pending"))
    monkeypatch.setattr(s, "metering_flush_interval_seconds", 3600.0)   # 只走显式 flush
    db.dispose()                       # 丢掉别的用例留下的引擎(URL 变了)
    install_writer(None)
    _clear(mysql)                      # 每个用例从空表开始
    real = MysqlStore()
    install_store(real)
    yield real
    install_store(None)
    install_writer(None)
    db.dispose()


# ---- 迁移与真表结构 ----


def test_migration_creates_the_four_tables(mysql, dsn):
    """up.sql 执行后四张表齐、引擎与字符集正确。"""
    names = set(inspect(mysql).get_table_names())
    assert set(TABLES) <= names

    rows = {r["TABLE_NAME"]: r for r in _query(mysql, """
        SELECT TABLE_NAME, ENGINE, TABLE_COLLATION FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
    """)}
    for table in TABLES:
        assert rows[table]["ENGINE"] == "InnoDB"
        assert (rows[table]["TABLE_COLLATION"] or "").startswith("utf8mb4")


def test_real_columns_match_the_models(mysql):
    """逐列比对模型与真库:可空性、长度、DECIMAL 精度、DATETIME(6) 一个都不能漂。"""
    from app.metering.tables import Base

    for table in Base.metadata.sorted_tables:
        live = _columns(mysql, table.name)
        model = {c.name: c for c in table.columns}
        assert set(live) == set(model), f"{table.name} 的列集合与模型不一致"

        for name, column in model.items():
            info = live[name]
            impl = column.type.dialect_impl(mysql_dialect.dialect())   # 变体 / 方言类型解析掉
            where = f"{table.name}.{name}"
            assert (info["IS_NULLABLE"] == "YES") == bool(column.nullable), f"{where} 可空性不符"
            if hasattr(impl, "length"):
                assert info["CHARACTER_MAXIMUM_LENGTH"] == impl.length, f"{where} 长度不符"
            if hasattr(impl, "precision"):
                assert (info["NUMERIC_PRECISION"], info["NUMERIC_SCALE"]) == (
                    impl.precision, impl.scale), f"{where} DECIMAL 精度不符"
            if isinstance(impl, mysql_dialect.DATETIME):
                assert info["DATETIME_PRECISION"] == 6, f"{where} 不是 DATETIME(6)"
            expected_int = {"INTEGER": "int", "BIGINT": "bigint", "SMALLINT": "smallint"}.get(
                impl.__visit_name__.upper())
            if expected_int is not None:                # 整数族只认这三档:INT 冒充 BIGINT 要拦住
                assert info["DATA_TYPE"] == expected_int, f"{where} 整数类型不符"


def test_keys_and_indexes_match_the_models(mysql):
    """主键 / 唯一约束 / 索引必须与模型一一对应(幂等就靠它们)。"""
    from sqlalchemy import UniqueConstraint

    from app.metering.tables import Base

    insp = inspect(mysql)
    for table in Base.metadata.sorted_tables:
        assert insp.get_pk_constraint(table.name)["constrained_columns"] == \
            [c.name for c in table.primary_key], f"{table.name} 主键不符"

        model_uq = {frozenset(u.columns.keys()) for u in table.constraints
                    if isinstance(u, UniqueConstraint)}
        live_uq_rows = insp.get_unique_constraints(table.name)
        assert {frozenset(u["column_names"]) for u in live_uq_rows} == model_uq, \
            f"{table.name} 唯一约束不符"

        model_ix = {i.name: tuple(c.name for c in i.columns) for i in table.indexes}
        live_ix = {i["name"]: tuple(i["column_names"]) for i in insp.get_indexes(table.name)}
        for name, cols in model_ix.items():
            assert live_ix.get(name) == cols, f"{table.name} 索引 {name} 缺失或列不符"
        # 库里多出来的索引也属于漂移(唯一约束会自带一个同名索引,按真人库里的列归拢后排除)
        allowed = set(model_ix.values()) | {tuple(u["column_names"]) for u in live_uq_rows}
        extra = {n: c for n, c in live_ix.items() if c not in allowed}
        assert not extra, f"{table.name} 有模型没声明的索引:{extra}"


def test_no_foreign_keys_so_deletes_never_cascade(mysql):
    """删文件 / 删索引任务 / 清缓存都不该级联删调用历史:表上不允许有任何外键。"""
    rows = _query(mysql, """
        SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL
    """)
    assert rows == []


def test_down_then_up_is_repeatable(mysql, dsn):
    """down.sql 要能真的把表删干净,再执行 up.sql 要能重建 —— 迁移不是单向的。"""
    _apply(dsn, "down")
    assert not (set(inspect(mysql).get_table_names()) & set(TABLES))

    _apply(dsn, "up")
    assert set(TABLES) <= set(inspect(mysql).get_table_names())


# ---- 写入:幂等 / Decimal / 事务 / 并发 ----


def test_insert_is_idempotent_and_money_round_trips(store, mysql):
    """补写重放不产生第二行;金额 / 用量 / 分摊原样进出,不变成浮点数。"""
    event = _priced(mk.embedding_call(usage_quantity=Decimal("1234.5"),
                                      items=(CallItem(oss_key="知识库/发票 🧾.pdf", page_no=1,
                                                      text_count=3,
                                                      allocated_cost=Decimal("0.00102800"),
                                                      allocation_note="按文本条数估算分摊"),)),
                    "0.00102800")

    assert store.insert([event], []) == 1
    assert store.insert([event], []) == 0                       # 重放:一行都不新增
    assert _count(mysql, "call_events") == 1
    assert _count(mysql, "call_event_items") == 1

    detail = store.get_call(event.event_id)
    assert detail["cost_amount"] == "0.00102800"                # DECIMAL(18,8) 原样
    assert detail["usage_quantity"] == "1234.500000"            # DECIMAL(20,6) 原样
    assert detail["items"][0]["allocated_cost"] == "0.00102800"
    assert detail["items"][0]["oss_key"] == "知识库/发票 🧾.pdf"   # utf8mb4 四字节字符

    summary = store.summary(CallFilter())
    money = {(c["service"], c["currency"]): c["amount"] for c in summary["cost_by_service_currency"]}
    assert money[(event.service, "CNY")] == "0.00102800"        # 合计也没有浮点误差
    assert summary["totals"]["calls"] == 1


def test_microseconds_and_utc_window_survive(store, mysql):
    """DATETIME(6) 的微秒必须留在库里(写成 DATETIME 会把同一秒内的多次尝试压平),
    并且 since 含 / until 不含的窗口语义在真库上同样成立。"""
    base = datetime(2026, 10, 3, 12, 0, 0, 123456, tzinfo=UTC)
    first = mk.embedding_call(occurred_at=base, usage_quantity=Decimal(10))
    second = mk.embedding_call(occurred_at=base + timedelta(seconds=1), usage_quantity=Decimal(10))
    store.insert([first, second], [])

    stored = _query(mysql, "SELECT occurred_at FROM call_events WHERE event_id = :e",
                    e=first.event_id)[0]["occurred_at"]
    assert stored.microsecond == 123456

    window = CallFilter(since=base, until=base + timedelta(seconds=1))
    rows = store.list_calls(window, offset=0, limit=10)["items"]
    assert [r["event_id"] for r in rows] == [first.event_id]     # 上界不含,下界含
    assert store.list_calls(CallFilter(since=base), offset=0, limit=10)["total"] == 2


def test_price_rule_unique_and_transaction_rollback(store, mysql):
    """价目唯一约束真的拦得住重复;失败的事务不留半截数据。"""
    insert = text("""
        INSERT INTO price_config (service, provider, target, unit, currency, unit_price,
                                  effective_from, source, note, created_at)
        VALUES (:service, :provider, :target, :unit, :currency, :unit_price,
                :effective_from, :source, :note, :created_at)
    """)
    row = dict(service="embedding", provider="dashscope", target="text-embedding-v4",
               unit="1k_tokens", currency="CNY", unit_price=Decimal("0.000514"),
               effective_from=datetime(2026, 10, 1, 0, 0, tzinfo=UTC).replace(tzinfo=None),
               source="官方价格页(测试)", note="", created_at=datetime.now(UTC).replace(tzinfo=None))
    with mysql.begin() as conn:
        conn.execute(insert, row)
    with pytest.raises(IntegrityError):
        with mysql.begin() as conn:                            # 同一规则(含生效时刻)再来一条
            conn.execute(insert, row)
    assert _count(mysql, "price_config") == 1

    with pytest.raises(RuntimeError):                          # 事务中段失败 → 整条回滚
        with mysql.begin() as conn:
            conn.execute(insert, {**row, "target": "另一个模型"})
            raise RuntimeError("模拟业务异常")
    assert _count(mysql, "price_config") == 1


def test_store_price_insert_delete_round_trip(store, mysql):
    """接口用的价目写路径在真库上成立:重复 → PriceExists(不覆盖),删除幂等地如实返回。"""
    from app.metering.store import PriceExists

    row = dict(service="ocr", provider="aliyun", target="Invoice", unit="request",
               currency="CNY", unit_price=Decimal("0.00700000"),
               effective_from=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
               source="官方价格页(测试)", note="")
    created = store.insert_price(row)
    assert created["id"] and created["unit_price"] == "0.00700000"     # DECIMAL(18,8) 原样
    assert created["effective_from"].startswith("2026-10-01T00:00:00")
    assert _count(mysql, "price_config") == 1

    with pytest.raises(PriceExists):          # 同一规则重复录入:拒绝而不是覆盖旧价
        store.insert_price(row)
    assert _count(mysql, "price_config") == 1

    assert store.delete_price(int(created["id"])) is True
    assert store.delete_price(int(created["id"])) is False             # 已经没了:如实返回
    assert _count(mysql, "price_config") == 0


def test_concurrent_identical_writes_insert_once(store, mysql):
    """多线程同时补写同一批事件:最终一人一行、不重不漏。

    并发下的瞬时死锁(1213)允许重试一次:生产侧写库失败会转入补写目录再来,
    这里给同样的第二次机会 —— 要守的是最终一致性,不是「一次也不许失败」。
    """
    def insert_with_retry(events, attempts: int = 3) -> int:
        for i in range(attempts):
            try:
                return store.insert(events, [])
            except OperationalError:
                if i == attempts - 1:
                    raise
                time.sleep(0.05 * (i + 1))
        return 0

    batch = [mk.embedding_call() for _ in range(10)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: insert_with_retry(batch), range(8)))

    assert sum(results) == 10                       # 一条只被「新插入」一次,其余都是重复分支
    assert _count(mysql, "call_events") == 10

    second = [mk.embedding_call() for _ in range(10)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: insert_with_retry(second), range(8)))
    assert sum(results) == 10 and _count(mysql, "call_events") == 20


# ---- 数据库故障 → 补写目录 → 回填(真库上的端到端) ----


def test_writer_flushes_spills_and_backfills_against_real_mysql(store, dsn, monkeypatch):
    """写不通就落补写目录,库回来了再补写,且补写只写日志、不重做业务调用、不产生重复行。"""
    from app.config import get_settings
    from app.metering import db, get_writer, install_writer
    from app.metering.writer import MeteringWriter

    writer = MeteringWriter()
    install_writer(writer)
    s = get_settings()
    try:
        ok = mk.embedding_call(usage_quantity=Decimal(100))
        writer.call(ok)
        assert writer.flush(timeout=10)["written"] == 1
        assert store.get_call(ok.event_id) is not None           # 正常路径:队列 → MySQL

        # 把地址换成一个连不上的库:写入失败 → 事件必须落盘而不是消失
        monkeypatch.setattr(s, "metering_mysql_url",
                            "mysql+pymysql://metering:never-used@127.0.0.1:1/qa_agent_scratch")
        down = mk.embedding_call(usage_quantity=Decimal(100))
        writer.call(down)
        writer.flush(timeout=10)
        status = writer.status()
        assert status["spilled"] == 1 and status["pending"] == 1
        assert _count(mysql, "call_events") == 1                  # 故障期间确实没写进去

        # 数据库回来:补写目录里的事件要进库,且只进一次
        monkeypatch.setattr(s, "metering_mysql_url", dsn)
        db.dispose()
        writer.flush(timeout=10)
        status = writer.status()
        assert status["backfilled"] == 1 and status["pending"] == 0 and status["lost"] == 0
        assert store.get_call(down.event_id) is not None
        assert _count(mysql, "call_events") == 2
        assert _count(mysql, "call_events") == len(mk.calls(store))     # 没有孤儿行
    finally:
        writer.stop(timeout=10)
        install_writer(None)
        db.dispose()
