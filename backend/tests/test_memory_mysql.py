"""MySQL 专属集成测试：长期记忆六张表（0004 + 0005 + 0006）的真结构 / 真列宽 / 微秒 / 并发认领 / 代次并发 / 作用域隔离 / fencing。

**默认全部跳过**：没设 MEMORY_TEST_MYSQL_URL 时一行都不连。SQLite 上的绿灯不能冒充
「MySQL 已验证」，所以本文件在没有 MySQL 的环境里只负责说清「没验」，不假装通过。
设了也必须指向可弃的测试库：库名里不含 test / scratch / tmp / dev 直接判失败，
免得有人把 DSN 指到 qa_agent_prod 上让测试建表 / 删表。

跑法（库先由 DBA / 自己建好，账号只要有建表权限即可，不需要 root；**DSN 记得带
charset=utf8mb4**，否则中文正文按 latin1 出入库）：
    set MEMORY_TEST_MYSQL_URL=mysql+pymysql://user:pw@host:3306/qa_agent_dev?charset=utf8mb4
    python -m pytest tests/test_memory_mysql.py -q

按序应用 **0004 → 0005 → 0006**（不碰 0001–0003，也不碰计量与认证的表），回滚按 0006 → 0005 → 0004 逆序：
所以可以对着已经跑过前三个迁移的开发库跑 —— 六张记忆表由 up.sql 建好、跑完由 down.sql
删掉；进去发现这六张表已经存在就判失败：宁可换一个空库，也不在别人正在用的库上练迁移。

并发用例是本文件存在的另一个理由：「同一用户同一时刻只能有一个任务在跑」与「代次不丢
不重」这两条保证都落在 InnoDB 的行锁上，而 SQLite 连 `FOR UPDATE` 都不渲染 —— 在 SQLite
上它们**看起来**成立，只是因为那里根本压不出并发。作用域隔离与 fencing（条件更新必须
同时匹配 owner + claim_token）同样只有真库能验：SQLite 上 UPDATE 的行数与并发语义都不作数。
"""

from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.memory import repo
from app.memory.errors import MemoryLeaseLost
from app.memory.models import (JOB_KIND_EXTRACT, JOB_RUNNING, OP_PURGE_USER, SCOPE_FORMAL,
                               STATUS_ACTIVE, eval_scope)
from app.memory.tables import Base
from tests import migrationkit as mig

TABLES = ("memory_items", "memory_history", "memory_jobs", "memory_user_state",
          "memory_sources", "memory_ops")
VERSIONS = (4, 5, 6, 8)                 # 按序应用;回滚逆序
ENV = "MEMORY_TEST_MYSQL_URL"

USER = "00000000-0000-4000-8000-00000000aa01"
NOW = datetime(2026, 10, 8, 12, 0, 0, 123456)          # 带微秒：DATETIME(6) 的往返就靠它检验
BODY = ("用户偏好用中文写实验记录，发票要按项目分摊 🧾" * 40)[:800]

# 库名必须自证「可弃」：qa_agent_dev / qa_agent_test / xxx_scratch 都行，qa_agent_prod 不行。
_SCRATCH_NAME = re.compile(r"(^|[_-])(test|testing|scratch|tmp|dev)([_-]|$)")


def _dsn() -> str:
    raw = (os.environ.get(ENV) or "").strip()
    if not raw:
        pytest.skip(f"未设置 {ENV}：MySQL 集成项未验证（SQLite 通过不等于 MySQL 通过）")
    database = (make_url(raw).database or "")
    if not _SCRATCH_NAME.search(database.lower()):
        pytest.fail(f"{ENV} 指向的库 {database!r} 不像测试库：拒绝在它上面建表 / 删表"
                    "（请用 qa_agent_dev，或 *_test / *_scratch 结尾的库）", pytrace=False)
    return raw


def _apply(dsn: str, direction: str) -> None:
    """按序执行本组迁移这两个版本（逐条 exec，等价于人手 `mysql <db> < 文件`）。

    与 test_metering_mysql 的差别就在这个「只」：那组迁移建的是计量表，这组建的是记忆表，
    各自只动自己那几张 —— 于是同一台开发库上两组用例都能跑，互不拆台。
    **升级按 0004 → 0005 → 0006，回滚按 0006 → 0005 → 0004**（逆序）—— 顺序错了 0005 的增量列就加不上。
    """
    items = [m for m in mig.migrations(direction) if m.version in VERSIONS]
    assert len(items) == len(VERSIONS), f"migrations/ 下不齐 {VERSIONS} 的 {direction} 文件"
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
    """探测 / 断言用的引擎：NullPool，用完即断，不给远端测试库留常驻连接。"""
    return create_engine(dsn, poolclass=NullPool)


@contextmanager
def _transaction(engine):
    """一次独立事务：提交即结束（行锁在提交时释放，并发用例全靠这一点）。"""
    session = Session(bind=engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _query(engine, sql: str, **params) -> list[dict]:
    with engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(text(sql), params)]


def _count(engine, table: str, where: str = "") -> int:
    sql = f"SELECT COUNT(*) AS n FROM {table}" + (f" WHERE {where}" if where else "")
    return _query(engine, sql)[0]["n"]


def _columns(engine, table: str) -> dict[str, dict]:
    """information_schema 里的真实列信息（建表语句写在迁移里，这里读的是库认定的事实）。"""
    rows = _query(engine, """
        SELECT COLUMN_NAME, IS_NULLABLE, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
               DATETIME_PRECISION
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table
    """, table=table)
    return {r["COLUMN_NAME"]: r for r in rows}


def _clear(engine) -> None:
    with engine.begin() as conn:
        for table in ("memory_history", "memory_ops", "memory_sources", "memory_jobs",
                      "memory_user_state", "memory_items"):
            conn.execute(text(f"DELETE FROM {table}"))


def _run_together(engine, count: int, worker, timeout: float = 30.0) -> list:
    """count 个线程在同一个屏障后一起冲，收集结果；线程里的异常原样抛给用例。"""
    barrier = threading.Barrier(count)
    results: list = [None] * count
    errors: list = []

    def run(index: int) -> None:
        try:
            barrier.wait(timeout=timeout)
            results[index] = worker(index)
        except Exception as exc:  # noqa: BLE001 —— 收集起来由用例原样抛，别吞成 None
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=count) as pool:
        list(pool.map(run, range(count)))
    if errors:
        raise errors[0]
    return results


def _claim_once(engine, owner: str, *, scope: str = SCOPE_FORMAL,
                now: datetime = NOW) -> dict | None:
    """一次独立事务的认领：每个线程各一条连接，这才是两台 worker 抢任务的真实姿态。

    返回**普通 dict**（id + claim_token），不是 ORM 对象：事务一提交 session 就关了，
    拿个 detached 行回去读属性会直接抛 DetachedInstanceError。claim_token 要带出来 ——
    fencing 用例后面正是用它证明「旧认领收不了尾」。
    """
    with _transaction(engine) as session:
        job = repo.claim_job(session, owner=owner, now=now, lease_seconds=60.0, scope=scope)
        return None if job is None else {"id": job.id, "claim_token": job.claim_token}


def _enqueue_op(engine, *, user_id: str, scope: str = SCOPE_FORMAL, generation: int = 0,
                kind: str = OP_PURGE_USER) -> str:
    with _transaction(engine) as session:
        return repo.enqueue_op(session, user_id=user_id, kind=kind, now=NOW, scope=scope,
                               generation=generation, max_attempts=3)


def _bump_once(engine) -> int:
    with _transaction(engine) as session:
        return repo.bump_generation(session, USER, now=NOW)


def _enqueue(engine, *, user_id: str, dedupe_key: str, payload: dict | None = None,
             scope: str = SCOPE_FORMAL, turn_id: str | None = None) -> str | None:
    with _transaction(engine) as session:
        return repo.enqueue_job(session, user_id=user_id, kind=JOB_KIND_EXTRACT,
                                dedupe_key=dedupe_key, thread_id="t-1",
                                payload=payload if payload is not None else {"messages": []},
                                max_attempts=3, now=NOW, scope=scope, turn_id=turn_id)


# ---- 护栏本身也要有测试：指到生产库名必须判失败，而不是悄悄跳过或照跑 ----


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


# ---- 夹具：库 → 0004 + 0005 + 0006 → 干净的六张表 ----


@pytest.fixture(scope="module")
def dsn() -> str:
    return _dsn()


@pytest.fixture(scope="module")
def mysql(dsn):
    """（已跑过 0001–0003 的）库 → 按序执行 0004、0005、0006 up.sql → 用例；跑完逆序 down.sql 清场。"""
    engine = _engine(dsn)
    clash = sorted(set(inspect(engine).get_table_names()) & set(TABLES))
    engine.dispose()
    if clash:
        pytest.fail(f"测试库 {make_url(dsn).database} 里已经有 {clash}："
                    "请换一个还没跑过 0004 / 0005 的库（或先手工清掉这六张表），"
                    "别拿正在用的库练迁移", pytrace=False)

    _apply(dsn, "up")

    engine = _engine(dsn)
    yield engine
    engine.dispose()
    _apply(dsn, "down")          # 清场失败会直接抛，同样让用例红


@pytest.fixture
def clean(mysql) -> object:
    """每个用例从空的六张表开始（表结构由模块夹具建，是共享状态）。"""
    _clear(mysql)
    return mysql


# ---- 迁移与真表结构 ----


def test_migration_creates_the_six_tables(clean):
    """0004 + 0005 + 0006 执行后六张表齐、引擎与字符集正确。"""
    names = set(inspect(clean).get_table_names())
    assert set(TABLES) <= names

    rows = {r["TABLE_NAME"]: r for r in _query(clean, """
        SELECT TABLE_NAME, ENGINE, TABLE_COLLATION FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME IN ('memory_items','memory_history','memory_jobs','memory_user_state',
                             'memory_sources','memory_ops')
    """)}
    for table in TABLES:
        assert rows[table]["ENGINE"] == "InnoDB"
        assert (rows[table]["TABLE_COLLATION"] or "").startswith("utf8mb4")


def test_the_0005_columns_are_not_null_and_have_no_default(clean):
    """0005 的增量列在真库上的形态：加完列后**不允许为空、也没有默认值**。

    这是「作用域与认领凭证不许缺省」的落地形态：留一个 DEFAULT '' 的话，某条漏写
    scope 的 INSERT 会静默落进正式数据（或某个 UPDATE 静默丢掉 claim_token），
    两种都是「隔离/fencing 静默失效」。模型里也没有 server_default，这里在库上钉死。
    """
    rows = {r["COLUMN_NAME"]: r for r in _query(clean, """
        SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'memory_items'
          AND COLUMN_NAME IN ('scope','generation','meta_version','indexed_revision',
                              'indexed_meta_version','kind')
    """)}
    assert set(rows) == {"scope", "generation", "meta_version", "indexed_revision",
                         "indexed_meta_version", "kind"}
    for name, info in rows.items():
        assert info["IS_NULLABLE"] == "NO", f"memory_items.{name} 允许为空"
        assert info["COLUMN_DEFAULT"] is None, f"memory_items.{name} 带了默认值"

    job_cols = {r["COLUMN_NAME"]: r for r in _query(clean, """
        SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'memory_jobs'
          AND COLUMN_NAME IN ('scope','claim_token','turn_id')
    """)}
    assert job_cols["scope"]["IS_NULLABLE"] == "NO" and job_cols["scope"]["COLUMN_DEFAULT"] is None
    assert (job_cols["claim_token"]["IS_NULLABLE"] == "NO"
            and job_cols["claim_token"]["COLUMN_DEFAULT"] is None)


def test_real_columns_match_the_models(clean):
    """逐列比对模型与真库：可空性、长度、整数档位、DATETIME(6) 一个都不能漂。

    SQLite 不校验长度、也没有 DATETIME(6)，所以「列宽与微秒都对」这句话只有在真库上
    才算数 —— 生产上正是 CHAR(32) 装不下 36 位 UUID 才栽过（见 test_metering_mysql）。
    """
    for table in Base.metadata.sorted_tables:
        live = _columns(clean, table.name)
        model = {c.name: c for c in table.columns}
        assert set(live) == set(model), f"{table.name} 的列集合与模型不一致"

        for name, column in model.items():
            info = live[name]
            impl = column.type.dialect_impl(mysql_dialect.dialect())   # 变体 / 方言类型解析掉
            where = f"{table.name}.{name}"
            assert (info["IS_NULLABLE"] == "YES") == bool(column.nullable), f"{where} 可空性不符"
            if isinstance(impl, mysql_dialect.TEXT):
                assert info["DATA_TYPE"] == "text", f"{where} 不是 TEXT：{info['DATA_TYPE']}"
            elif hasattr(impl, "length"):
                assert info["CHARACTER_MAXIMUM_LENGTH"] == impl.length, f"{where} 长度不符"
            if isinstance(impl, mysql_dialect.DATETIME):
                assert info["DATETIME_PRECISION"] == 6, f"{where} 不是 DATETIME(6)"
            expected_int = {"INTEGER": "int", "BIGINT": "bigint", "SMALLINT": "smallint"}.get(
                impl.__visit_name__.upper())
            if expected_int is not None:            # 整数族只认这三档：INT 冒充 BIGINT 要拦住
                assert info["DATA_TYPE"] == expected_int, f"{where} 整数类型不符"


def test_keys_and_indexes_match_the_models(clean):
    """主键 / 唯一约束 / 索引必须与模型一一对应（任务去重就靠那条唯一键）。"""
    from sqlalchemy import UniqueConstraint

    insp = inspect(clean)
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
        # 库里多出来的索引也属于漂移（唯一键会自带一个同名索引，按列归拢后排除）
        allowed = set(model_ix.values()) | {tuple(u["column_names"]) for u in live_uq_rows}
        extra = {n: c for n, c in live_ix.items() if c not in allowed}
        assert not extra, f"{table.name} 有模型没声明的索引：{extra}"


def test_no_foreign_keys_so_a_purge_never_cascades(clean):
    """「彻底删除」要自己决定删什么（任务连 payload 一起删、审计只脱敏）：表上不许有外键。"""
    rows = _query(clean, """
        SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL
          AND TABLE_NAME IN ('memory_items','memory_history','memory_jobs','memory_user_state',
                             'memory_sources','memory_ops')
    """)
    assert rows == []


def test_down_then_up_is_repeatable(clean, dsn):
    """down.sql 要能真的把六张表删干净，再按序执行 up.sql 要能重建 —— 迁移不是单向的。"""
    _apply(dsn, "down")
    assert not (set(inspect(clean).get_table_names()) & set(TABLES))

    _apply(dsn, "up")
    assert set(TABLES) <= set(inspect(clean).get_table_names())


# ---- 写入：正文 / 微秒 / JSON 原样进出，超长真的被挡住 ----


def test_text_and_microseconds_round_trip(clean):
    """800 字中文正文（含 4 字节 emoji）与 DATETIME(6) 的微秒原样往返。

    中文读回来是乱码时先看 DSN 有没有 `?charset=utf8mb4`；微秒被吞掉则说明列不是
    DATETIME(6)（写成 DATETIME 会把同一秒内的多次变更压平，审计就对不上顺序了）。
    """
    with _transaction(clean) as session:
        item_id = repo.create_item(session, user_id=USER, text=BODY, now=NOW).id

    with _transaction(clean) as session:              # 换一条连接读：确实进了库
        got = repo.get_item(session, USER, item_id)

    assert got is not None
    assert got.text == BODY and len(got.text) == 800
    assert got.created_at == NOW                      # 微秒 123456 没有被压成秒
    assert got.created_at.microsecond == 123456
    assert got.status == STATUS_ACTIVE


def test_strict_mode_really_bounds_the_columns(clean):
    """超长值必须**报错**，而不是被悄悄截断（CHAR(32)/CHAR(36) 装不下时会报 1406）。

    SQLite 根本不看长度，这一条只有真库挡得住；反过来说，若这台 MySQL 没开 STRICT 模式，
    它会静默截断 —— 那本用例会失败，这正是希望被你看见的事。
    """
    with pytest.raises(DBAPIError, match="Data too long"):
        with _transaction(clean) as session:          # user_id 是 CHAR(36)，这里给 37 位
            repo.create_item(session, user_id="x" * 37, text="超长归属", now=NOW)


def test_job_payload_json_round_trips_and_is_cleared_on_finish(clean):
    """任务 payload（JSON 列）原样进出；进终态时清成 {} ——正文不在库里长留。"""
    payload = {"messages": [{"role": "user", "content": "我住在深圳 🏠"}], "note": None}
    job_id = _enqueue(clean, user_id=USER, dedupe_key="k1", payload=payload)

    with _transaction(clean) as session:
        assert repo.get_job(session, job_id).payload == payload

    with _transaction(clean) as session:
        # 不是我领的：owner 对不上、凭证也不对 —— 条件更新一行都不该动
        assert repo.finish_job(session, job_id=job_id, owner="w0", claim_token="0" * 32,
                               now=NOW) is False
    with _transaction(clean) as session:
        claimed = repo.claim_job(session, owner="w0", now=NOW, lease_seconds=60.0)
        assert claimed is not None and claimed.payload == payload
        assert repo.finish_job(session, job_id=job_id, owner="w0",
                               claim_token=claimed.claim_token, now=NOW) is True

    with _transaction(clean) as session:
        done = repo.get_job(session, job_id)
    assert done.payload == {} and done.status == "succeeded"


def test_purge_clears_the_rows_and_masks_the_audit(clean):
    """「彻底删除」在真库上：记忆行、任务行（含 payload）连根拔，审计行保留但正文置 NULL。"""
    _enqueue(clean, user_id=USER, dedupe_key="k1", payload={"messages": [{"content": "秘密"}]})
    with _transaction(clean) as session:
        repo.create_item(session, user_id=USER, text="我住在深圳", now=NOW)

    with _transaction(clean) as session:
        result = repo.purge_user(session, USER, now=NOW)

    assert (result.items, result.jobs, result.generation) == (1, 1, 1)
    assert _count(clean, "memory_items") == 0
    assert _count(clean, "memory_jobs") == 0
    assert _query(clean, "SELECT payload FROM memory_jobs") == []      # 任务里的对话正文也走了
    rows = _query(clean, "SELECT old_text, new_text, reason FROM memory_history")
    assert rows and all(r["old_text"] is None and r["new_text"] is None for r in rows)
    assert {r["reason"] for r in rows} == {"用户数据彻底删除"}
    assert _count(clean, "memory_user_state") == 1                      # 水位只增不减


def test_the_first_write_creates_the_generation_row(clean):
    """首次写入顺带建出代次行（generation=0）：**有行才有锁**，清除才能与写入串行。"""
    with _transaction(clean) as session:
        repo.create_item(session, user_id=USER, text="我住在深圳", now=NOW)
        assert repo.lock_user_state(session, USER, now=NOW) == 0        # 建行 + 加锁

    assert _count(clean, "memory_user_state", f"user_id = '{USER}'") == 1
    assert _count(clean, "memory_user_state") == 1


# ---- 并发：认领与代次（SQLite 上压不出来，只能在这里压）----


def test_concurrent_claims_of_one_job_win_once(clean):
    """八个 worker 抢同一个任务：正好一个赢，attempts 只 +1（输的那些不许动计数）。"""
    job_id = _enqueue(clean, user_id=USER, dedupe_key="k1")

    claimed = _run_together(clean, 8, lambda i: _claim_once(clean, f"w{i}"))

    won = [c["id"] for c in claimed if c]
    assert won == [job_id], f"同一任务被多次认领：{claimed}"
    row = _query(clean, "SELECT status, attempts FROM memory_jobs")[0]
    assert (row["status"], row["attempts"]) == (JOB_RUNNING, 1)


def test_two_jobs_of_one_user_are_never_claimed_together(clean):
    """同一用户的两个待执行任务，两个 worker 同时抢：**只能有一个赢**。

    这是「多 worker 不同时维护同一用户」的 MySQL 侧验证，也是 claim_job 里那把用户代次
    行锁的存在理由：只靠「先查一遍谁在跑」的快照读，两台 worker 会各自看到「没人在跑」，
    然后各自领走一个任务 —— 而维护决策要在「读到的现有记忆」上做，两边同时跑就会同时
    ADD 出重复行。第二个任务只能等第一个跑完（那时它才不是 running）。
    """
    first = _enqueue(clean, user_id=USER, dedupe_key="k1")
    second = _enqueue(clean, user_id=USER, dedupe_key="k2")
    assert first and second

    claimed = _run_together(clean, 4, lambda i: _claim_once(clean, f"w{i}"))

    won = [c for c in claimed if c]
    assert len(won) == 1, f"同一用户被同时领走两个任务：{claimed}"
    assert _count(clean, "memory_jobs", "status = 'running'") == 1
    assert _count(clean, "memory_jobs", "status = 'pending'") == 1       # 另一个还在排队


def test_a_running_job_does_not_block_another_user(clean):
    """串行只针对同一个用户：别人在跑不影响我 —— 一个人卡住不该拖住整条队列。"""
    other = "00000000-0000-4000-8000-00000000aa02"
    _enqueue(clean, user_id=USER, dedupe_key="k1")
    _enqueue(clean, user_id=other, dedupe_key="k2")

    claimed = _run_together(clean, 2, lambda i: _claim_once(clean, f"w{i}"))

    assert len([c for c in claimed if c]) == 2, f"两个不同用户的任务应当都能被领走：{claimed}"
    assert _count(clean, "memory_jobs", "status = 'running'") == 2


def test_concurrent_purges_do_not_lose_or_repeat_a_generation(clean):
    """五个线程同时「彻底删除」：代次恰好走过 1..5，行只有一行。

    代次是「旧任务作废」的唯一依据，丢一次就意味着某次清除对旧任务失效。第一条路径
    （行不存在 → 建行 + 加锁）正是 MySQL 上最容易踩死锁的地方：先 `SELECT ... FOR UPDATE`
    查不存在的行会留下间隙锁，再各自 INSERT 就互相等 —— 所以建行用的是 upsert（见
    repo._ensure_state_row）。本用例同时在防「丢代次」和「死锁」这两件事。
    """
    assert _count(clean, "memory_user_state") == 0      # 从「还没有代次行」开始

    seen = _run_together(clean, 5, lambda i: _bump_once(clean))

    assert sorted(seen) == [1, 2, 3, 4, 5], f"代次丢档或重复：{seen}"
    assert _count(clean, "memory_user_state") == 1
    with _transaction(clean) as session:
        assert repo.generation_of(session, USER) == 5


# ---- 作用域隔离、fencing 与清理台账（0005 的主保证，只有真库上的过滤与条件更新算数）----


EVAL_USER = "00000000-0000-4000-8000-00000000aa11"


def test_two_scopes_never_claim_each_others_jobs(clean):
    """正式 Worker 只领 `scope=''`、评测 Worker 只领自己那个 scope —— 认领的过滤在真库上成立。

    用**不同用户**是刻意的:同用户串行是跨 scope 的保护(见 repo._has_running_job,它不按
    scope 过滤),那样会盖住这里真正要验的东西 —— 「scope 是不是硬过滤条件」。
    """
    run = eval_scope("smoke-0001")
    formal = _enqueue(clean, user_id=USER, dedupe_key="k1")
    evaled = _enqueue(clean, user_id=EVAL_USER, dedupe_key="k2", scope=run)

    ev = _claim_once(clean, "w-eval", scope=run)
    assert ev and ev["id"] == evaled, f"评测 Worker 没领到自己的任务:{ev}"
    assert _claim_once(clean, "w-eval", scope=run) is None       # 它那份只有这一个任务

    fm = _claim_once(clean, "w-formal")                          # 正式 Worker(只认 scope='')
    assert fm and fm["id"] == formal, f"正式 Worker 领到了别人的任务:{fm}"

    # 反过来也一样:评测 Worker 再要一次,不该拿到正式任务;正式 Worker 也拿不到评测的
    assert _claim_once(clean, "w-eval", scope=run) is None
    assert _claim_once(clean, "w-formal") is None
    assert _count(clean, "memory_jobs", f"scope = '{run}' AND status = 'running'") == 1
    assert _count(clean, "memory_jobs", "scope = '' AND status = 'running'") == 1


def test_requeue_expired_stays_inside_its_scope(clean):
    """租约过期回收同样按作用域过滤:评测 Worker 回收不到正式任务(反之亦然)。"""
    run = eval_scope("smoke-0002")
    formal = _enqueue(clean, user_id=USER, dedupe_key="k1")
    evaled = _enqueue(clean, user_id=EVAL_USER, dedupe_key="k2", scope=run)
    assert _claim_once(clean, "w", scope=run) and _claim_once(clean, "w")

    later = NOW + timedelta(seconds=120)                          # 60 秒租约都过期了
    with _transaction(clean) as session:
        assert repo.requeue_expired(session, now=later, scope=run) == 1

    rows = {r["id"]: r["status"] for r in _query(clean, "SELECT id, status FROM memory_jobs")}
    assert rows[evaled] == "pending", "评测任务没有被放回"
    assert rows[formal] == JOB_RUNNING, "正式任务的租约被评测那份回收动了"


def test_a_taken_over_job_can_no_longer_be_finished_by_the_stale_claim(clean):
    """接管之后,旧认领连「收尾」都做不了 —— 条件更新必须同时匹配 owner + claim_token。

    这就是「失去租约的执行者不许把任务标成功」在真库上的形态:少了凭证这一维,旧执行者
    照 job_id 就能把任务改成 succeeded(甚至清掉 payload),而它可能一次事实都没提交过。
    """
    payload = {"messages": [{"role": "user", "content": "别被旧认领清掉"}]}
    _enqueue(clean, user_id=USER, dedupe_key="k1", payload=payload)
    stale = _claim_once(clean, "w-stale")
    assert stale

    later = NOW + timedelta(seconds=120)
    with _transaction(clean) as session:
        assert repo.requeue_expired(session, now=later) == 1
    fresh = _claim_once(clean, "w-fresh", now=later)
    assert fresh and fresh["id"] == stale["id"]
    assert fresh["claim_token"] != stale["claim_token"], "重新认领没有换凭证"

    with _transaction(clean) as session:                          # 提交前的 fencing 校验
        with pytest.raises(MemoryLeaseLost):
            repo.assert_claim_valid(session, user_id=USER, job_id=stale["id"],
                                    owner="w-stale", claim_token=stale["claim_token"],
                                    generation=None, now=later)
    with _transaction(clean) as session:                          # 收尾与续租同样是条件更新
        assert repo.finish_job(session, job_id=stale["id"], owner="w-stale",
                               claim_token=stale["claim_token"], now=later) is False
        assert repo.renew_lease(session, job_id=stale["id"], owner="w-stale",
                                claim_token=stale["claim_token"], now=later,
                                lease_seconds=60.0) is False

    row = _query(clean, "SELECT status, lease_owner, payload FROM memory_jobs")[0]
    assert (row["status"], row["lease_owner"]) == (JOB_RUNNING, "w-fresh")
    # 旧认领的收尾**一行都没动**:payload 没被清成 {}(清 payload 是 finish_job 做的事)
    assert (row["payload"] or {}) == payload
    with _transaction(clean) as session:                          # 新的那次认领照常收尾
        assert repo.finish_job(session, job_id=fresh["id"], owner="w-fresh",
                               claim_token=fresh["claim_token"], now=later) is True


def test_a_scoped_purge_cannot_reach_the_other_scope(clean):
    """purge 按 (user_id, scope) 圈定:清评测作用域时,同一用户的正式行一行不动。

    评测用户虽然由 run-id 派生(跑不到真实账号上),这一层仍是结构上的第二道保险 ——
    「清评测」与「清正式」走的是同一个函数,区别只在 scope。
    """
    run = eval_scope("smoke-0003")
    with _transaction(clean) as session:
        formal_id = repo.create_item(session, user_id=USER, text="正式记忆", now=NOW).id
        eval_id = repo.create_item(session, user_id=USER, text="评测记忆", now=NOW,
                                   scope=run).id
        repo.add_sources(session, memory_ids=[formal_id], user_id=USER,
                         conversation_id="c-formal", turn_id="t-formal", now=NOW)
        repo.add_sources(session, memory_ids=[eval_id], user_id=USER, scope=run,
                         conversation_id="c-eval", turn_id="t-eval", now=NOW)
    _enqueue(clean, user_id=USER, dedupe_key="k-formal", turn_id="t-formal")
    _enqueue(clean, user_id=USER, dedupe_key="k-eval", scope=run, turn_id="t-eval")

    with _transaction(clean) as session:
        result = repo.purge_user(session, USER, now=NOW, scope=run)

    assert (result.items, result.jobs, result.sources) == (1, 1, 1)
    assert [r["text"] for r in _query(clean, "SELECT text FROM memory_items")] == ["正式记忆"]
    assert _count(clean, "memory_jobs") == 1
    assert _count(clean, "memory_sources") == 1
    assert _count(clean, "memory_sources", f"scope = '{run}'") == 0
    assert _count(clean, "memory_user_state") == 1                # 代次是全局的:只增不减


def test_ops_are_claimed_by_scope_and_finished_only_by_their_own_claim(clean):
    """清理台账的两条在真库上:按作用域认领 + 凭证条件收尾(凭证对不上就一行不动)。"""
    run = eval_scope("smoke-0004")
    evaled = _enqueue_op(clean, user_id=USER, scope=run)
    formal = _enqueue_op(clean, user_id=USER)

    with _transaction(clean) as session:
        claimed = repo.claim_op(session, owner="w-eval", now=NOW, lease_seconds=60.0, scope=run)
        assert claimed is not None and claimed.id == evaled, "评测 Worker 领到了正式台账"
        assert repo.finish_op(session, op_id=claimed.id, owner="w-eval",
                              claim_token="0" * 32, now=NOW) is False
        assert repo.finish_op(session, op_id=claimed.id, owner="w-eval",
                              claim_token=claimed.claim_token, now=NOW) is True

    assert _count(clean, "memory_ops", "status = 'succeeded'") == 1
    with _transaction(clean) as session:                          # 正式那份没被动过,可被认领
        later = repo.claim_op(session, owner="w-formal", now=NOW, lease_seconds=60.0)
    assert later is not None and later.id == formal
