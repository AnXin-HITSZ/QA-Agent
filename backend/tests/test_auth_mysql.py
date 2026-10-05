"""认证模块的 MySQL 专属行为:行锁(FOR UPDATE)/ 二进制排序规则 / 真长度标识 / 并发刷新。

**默认全部跳过**:没设 AUTH_TEST_MYSQL_URL 时一行都不连。SQLite 上的绿灯不能冒充
「MySQL 已验证」,所以本文件在没有 MySQL 的环境里只负责说清「没验」,不假装通过。
设了也必须指向可弃的测试库:库名里不含 test / scratch / tmp / dev 直接判失败,
免得有人把 DSN 指到 qa_agent_prod 上让测试建表 / 删表。约定与护栏和
tests/test_metering_mysql.py 一致(那边管计量表,这边管认证表)。

为什么这几条非真库不可:

- SQLite 方言会**静默丢掉** `SELECT ... FOR UPDATE`(SQLAlchemy 根本不渲染这段),
  于是「第二个写入者要等第一个提交」在那边永远成立,测了等于没测;
- `utf8mb4_bin` 是**列级排序规则**。SQLite 上只能注册一个同名 collation 去模拟
  (见 conftest.register_sqlite_collations)—— 那是为了让别的用例能建表,
  不等于验证了 MySQL 上排序规则真的不折叠大小写 / 重音;
- `CHAR` / `VARCHAR` 的长度在 SQLite 上不校验,36 位 UUID、顶格邮箱、四字节标题
  这些「真长度」只有真库会拦(生产上栽过 1406 Data too long)。

跑法(库先建好,账号只要有建表权限即可,不需要 root):
    set AUTH_TEST_MYSQL_URL=mysql+pymysql://user:pw@host:3306/qa_agent_dev?charset=utf8mb4
    python -m pytest tests/test_auth_mysql.py -q

表由 migrations/ 下的 up.sql 在开始时逐条执行建好、结束时 down.sql 删掉;若进去发现
表已存在,直接判失败(宁可让你换一个空库,也不在别人正在用的库上练迁移)。
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from tests import migrationkit as mig

ENV = "AUTH_TEST_MYSQL_URL"

# 库名必须自证「可弃」:qa_agent_dev / qa_agent_test / xxx_scratch 都行,qa_agent_prod 不行。
_SCRATCH_NAME = re.compile(r"(^|[_-])(test|testing|scratch|tmp|dev)([_-]|$)")


def _dsn() -> str:
    raw = (os.environ.get(ENV) or "").strip()
    if not raw:
        pytest.skip(f"未设置 {ENV}:MySQL 专属项(行锁 / utf8mb4_bin)未验证(SQLite 通过不等于 MySQL 通过)")
    database = (make_url(raw).database or "")
    if not _SCRATCH_NAME.search(database.lower()):
        pytest.fail(f"{ENV} 指向的库 {database!r} 不像测试库:拒绝在它上面建表 / 删表"
                    "(请用 qa_agent_dev,或 *_test / *_scratch 结尾的库)", pytrace=False)
    return raw


def _engine(dsn: str):
    """探测 / 断言用的引擎:NullPool,用完即断,不给远端测试库留常驻连接。"""
    return create_engine(dsn, poolclass=NullPool)


def _apply(dsn: str, direction: str) -> None:
    """把 migrations/ 下该方向的 SQL 全部执行一遍(等价于人手逐条 `mysql <db> < file`)。

    回滚按版本倒序执行(与上线相反);出错直接抛,由夹具暴露,不吞。
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


def _query(engine, sql: str, **params) -> list[dict]:
    with engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(text(sql), params)]


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


# ---- 夹具:库 → 迁移 → 干净的表 → 把应用指过去 ----


@pytest.fixture(scope="module")
def dsn() -> str:
    return _dsn()


@pytest.fixture(scope="module")
def mysql(dsn):
    """空库 → 执行全部 up.sql → 用例;跑完执行全部 down.sql 回空。"""
    engine = _engine(dsn)
    clash = sorted(set(inspect(engine).get_table_names()) & set(mig.migration_tables()))
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
def app_db(mysql, dsn, monkeypatch):  # noqa: ARG001 —— mysql 是前置:先建好表再把应用指过去
    """把应用指向测试库:后面的 store / AuthService 走**真引擎 + 真表**。

    各用例自己造带唯一后缀的数据,不用清表(数量级很小,互不干扰)。
    """
    from app.auth import db
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "metering_mysql_url", dsn)
    monkeypatch.setattr(s, "auth_jwt_secret", "auth-mysql-test-secret-" + "0" * 32)
    # 宽限窗口是本文件的重点之一:窗口为 0 时,并发刷新的后到者会被判成重放(见最后一条用例)。
    monkeypatch.setattr(s, "auth_refresh_grace_seconds", 30)
    db.dispose()                       # 丢掉别的用例留下的引擎(URL 变了)
    yield db
    db.dispose()


def _seed_active_user(db, *, email: str = "") -> str:
    """造一个「已验证 + 已审批」的账号(能登录、能刷新、能建会话)。"""
    from app.auth import store
    from app.auth import tokens
    from app.auth.passwords import hash_password

    user_id = tokens.new_id()
    now = db.utc_naive()
    with db.session_scope() as session:
        store.create_user(session, user_id=user_id,
                          email=email or f"auth-mysql-{user_id[:8]}@example.com",
                          password_hash=hash_password("Str0ng-Passw0rd!"),
                          display_name="MySQL 测试", now=now)
        store.mark_email_verified(session, user_id, now=now)
        store.set_review(session, user_id, admin_id=None, approve=True,
                         note="MySQL 测试夹具", now=now)
    return user_id


def _seed_session_with_token(db, *, digest: str) -> tuple[str, str]:
    """在已激活的账号上再挂一条会话 + 一枚刷新令牌行(刷新链的起点)。"""
    from app.auth import store
    from app.auth import tokens

    user_id = _seed_active_user(db)
    session_id = tokens.new_id()
    now = db.utc_naive()
    with db.session_scope() as session:
        store.create_session(session, session_id=session_id, user_id=user_id,
                             user_agent="pytest", ip="127.0.0.1", now=now,
                             expires_at=now + timedelta(days=30))
        store.add_refresh_token(session, token_id=tokens.new_id(), session_id=session_id,
                                user_id=user_id, token_hash=digest, now=now)
    return user_id, session_id


# ---- 排序规则:邮箱是精确匹配,不折叠大小写 / 重音 ----


def test_email_column_collation_is_binary(mysql):
    """users.email 的列排序规则必须是 utf8mb4_bin。

    退化成任何 *_ci(MySQL 8 默认 utf8mb4_0900_ai_ci):a@x.com 与 A@x.com、
    resume@x.com 与 résumé@x.com 会被判成**同一个地址** —— 唯一键互相冲突、查询互相串号。
    """
    rows = _query(mysql, """
        SELECT COLLATION_NAME FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'email'
    """)
    assert rows and rows[0]["COLLATION_NAME"] == "utf8mb4_bin", rows


def test_emails_differing_only_in_case_or_accent_are_distinct_accounts(app_db):
    """规范化只做「去空白 + 整体小写」,不替用户折叠大小写 / 重音 —— 它们是不同账号。"""
    from app.auth import store
    from app.auth import tokens
    from app.auth.passwords import hash_password

    nonce = tokens.new_id()[:8]
    plain, upper = f"{nonce}.a@x.com", f"{nonce}.A@x.com"
    base, accented = f"{nonce}.resume@x.com", f"{nonce}.résumé@x.com"
    now = app_db.utc_naive()
    with app_db.session_scope() as session:
        for email in (plain, upper, base, accented):   # _ci 排序规则下第 2、4 行会撞唯一键
            store.create_user(session, user_id=tokens.new_id(), email=email,
                              password_hash=hash_password("Str0ng-Passw0rd!"),
                              display_name="排序规则", now=now)
    with app_db.session_scope() as session:
        assert store.get_user_by_email(session, plain) is not None
        assert store.get_user_by_email(session, base) is not None
        assert store.get_user_by_email(session, upper) is None       # 不折叠大小写
        assert store.get_user_by_email(session, accented) is None    # 不折叠重音


# ---- 真长度:标识 / 标题 / 邮箱都得进得去、原样读得回 ----


def test_real_length_identifiers_emails_and_titles_round_trip(app_db):
    """36 位 UUID、顶格邮箱(254)、带四字节字符的长标题 —— SQLite 不校验长度,只有真库会拦。"""
    from app.auth import store
    from app.conversations import store as conv_store

    long_email = "a" * 64 + "@" + "b" * 185 + ".com"        # 254:邮箱列的上限
    assert len(long_email) == 254
    user_id = _seed_active_user(app_db, email=long_email)
    with app_db.session_scope() as session:
        assert store.get_user_by_email(session, long_email).id == user_id

    title = "🧾 发票丢了怎么办" + "很长的标题" * 20            # 会被截到列宽,但不是截断错误
    conv = asyncio.run(conv_store.create_for_user(user_id=user_id, title=title))
    assert conv_store.parse_thread_id(conv.thread_id) == (user_id, conv.id)   # 线程 id 没被截

    with app_db.session_scope() as session:
        row = store.get_conversation_by_thread_id(session, conv.thread_id)
        assert row is not None, "回查不到:线程 id 没原样进库(多半是列宽不够)"
        assert (row.user_id, row.status) == (user_id, store.CONV_ACTIVE)
        assert row.title == title[:conv_store.MAX_TITLE_LENGTH]
        assert store.get_conversation(session, conv.id).thread_id == conv.thread_id


def test_conversation_thread_id_unique_key_holds(app_db):
    """uk_conversations_thread 真拦得住重复线程 id(归属与寻址都指望着它)。"""
    from sqlalchemy.exc import IntegrityError

    from app.auth import store
    from app.conversations import store as conv_store

    user_id = _seed_active_user(app_db)
    conv = asyncio.run(conv_store.create_for_user(user_id=user_id, title="第一条"))
    now = app_db.utc_naive()
    with pytest.raises(IntegrityError):
        with app_db.session_scope() as session:            # 同一线程 id 再插一行 → 唯一键报错
            store.create_conversation(session, conv_id="another-id", user_id=user_id,
                                      thread_id=conv.thread_id, title="重复", now=now)


# ---- 行锁:同一枚刷新令牌被并发提交时,FOR UPDATE 把两个 worker 串起来 ----


def test_for_update_blocks_a_second_writer(app_db):
    """`FOR UPDATE` 要真的挡住第二个写入者 —— SQLite 上它是个空操作,只有真库能验证。"""
    from sqlalchemy import text as sql_text

    from app.auth import store
    from app.auth import tokens
    from app.metering import db as mysql_db

    _, digest = tokens.new_random_token()
    _seed_session_with_token(app_db, digest=digest)

    factory = mysql_db.session_factory()       # 两个**互相独立**的会话 = 两条连接
    holder, waiter = factory(), factory()
    try:
        holder.begin()
        assert store.find_refresh_token(holder, digest, for_update=True) is not None

        # 对照组:不加 FOR UPDATE 的普通读**不**被挡(证明接下来的阻塞来自行锁本身)
        assert store.find_refresh_token(waiter, digest) is not None

        waiter.execute(sql_text("SET SESSION innodb_lock_wait_timeout = 1"))   # 别让用例等 50 秒
        with pytest.raises(OperationalError) as excinfo:
            store.find_refresh_token(waiter, digest, for_update=True)
        assert excinfo.value.orig.args[0] == 1205, f"期望被行锁挡下(1205),实际:{excinfo.value!r}"

        waiter.rollback()                       # 超时只回滚了那一条语句,这里收拾干净
        holder.rollback()                       # 放锁
        assert store.find_refresh_token(waiter, digest, for_update=True) is not None
    finally:
        holder.close()
        waiter.close()


def test_concurrent_refresh_of_one_token_keeps_the_session_alive(app_db):
    """同一枚刷新令牌被两个请求同时提交(前端并发刷新 / 网络重发):两个都要成功,
    而且**不能**被当成令牌泄露而撤销整个会话。

    FOR UPDATE 把两次刷新串行化:后到的那次读到的已是「已消费」的行,落在宽限窗口内,
    于是照发一对新令牌(见 service._refresh_sync 的注释)。SQLite 上 FOR UPDATE 不生效,
    后到者读的还是自己的旧快照、CAS 更新失败,直接报「登录状态已失效」—— 这条只在真库上成立。
    """
    from app.auth import store
    from app.auth import tokens
    from app.auth.service import AuthService

    raw, digest = tokens.new_random_token()
    _, session_id = _seed_session_with_token(app_db, digest=digest)
    service = AuthService(now=app_db.utc_naive)

    async def both_at_once():
        return await asyncio.gather(
            service.refresh(refresh_token=raw, ip="127.0.0.1"),
            service.refresh(refresh_token=raw, ip="127.0.0.1"),
        )

    issued = asyncio.run(both_at_once())
    assert len(issued) == 2
    for item in issued:
        assert item.access_token and item.refresh_token
    with app_db.session_scope() as session:
        assert store.get_session(session, session_id).revoked_at is None, \
            "并发刷新不该被当成重放而撤销会话"
