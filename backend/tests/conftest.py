"""测试夹具:OSS 读写、Qdrant、Embeddings、OCR 全部换成离线假实现。

- SOP:接管 app.rag.oss.sops_store(内存假仓库,不触网);
- 知识库:接管 app.rag.oss.knowledge_store(内存假仓库,可改 mtime 模拟源文件变化);
- 向量库:QdrantClient(":memory:")(qdrant-client 自带,真实 API、零外部依赖);
- Embeddings:确定性假向量(维度对齐 EMBEDDINGS_DIM,便于校验集合维度);
- 缓存:进程内 MemoryCache(方案要求的「隔离的测试存储」),不碰真实 Redis;
- OCR:任何测试都不调用真实供应商 —— monkeypatch ocr.recognize_raw(网络边界,
  返回假响应 dict),两层缓存 / 归一化仍真实执行;识别结果全部由测试给定。
- .doc 转换:一律按「本机没装 soffice」处理,测试不真起 LibreOffice 子进程
  (见 _no_real_libreoffice;真转换只留 test_doc_convert.py 末尾装了才跑的一条)。
"""

from __future__ import annotations

import zlib

import pytest

from app.skills import loader

_FAKE_MTIME = 1_700_000_000  # 固定的最后修改时间戳,便于断言 updated_at


class FakeSopsStore:
    """仿 OssStore 的内存子集(CRUD 路由 + loader 所需),数据放内存 dict。"""

    prefix = "sops/"  # 对齐 OssStore.prefix,路由用它拼完整 key(sops/<id>.md)

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files: dict[str, str] = dict(files or {})  # {前缀相对 key: markdown 原文}

    def list_all(self, prefix: str = "") -> list[dict]:
        return [
            {"key": k, "size": len(v.encode("utf-8")), "last_modified": _FAKE_MTIME}
            for k, v in self.files.items()
        ]

    def get_object(self, key: str) -> bytes:
        return self.files[key].encode("utf-8")

    def object_exists(self, key: str) -> bool:
        return key in self.files

    def put_object(self, key: str, data: bytes, *, content_type=None, meta=None) -> str:
        self.files[key] = data.decode("utf-8") if isinstance(data, bytes) else data
        return self.prefix + key

    def delete_object(self, key: str) -> None:
        self.files.pop(key, None)

    def stat(self, key: str) -> dict | None:
        if key not in self.files:
            return None
        return {"size": len(self.files[key].encode("utf-8")), "last_modified": _FAKE_MTIME}


@pytest.fixture
def install_sops(monkeypatch):
    """安装内存 SOP:传 {文件名: 原文},接管 loader 的 sops_store 并 reload。

    返回假仓库对象——其 files 可后续增删,再调 loader.reload() 生效,模拟 OSS 内容变化。
    """
    store = FakeSopsStore()
    monkeypatch.setattr("app.rag.oss.sops_store", lambda: store)

    def _install(files: dict[str, str]) -> FakeSopsStore:
        store.files = dict(files)
        loader.reload()
        return store

    yield _install
    loader._load.cache_clear()  # 清理:避免假数据泄漏到不使用本夹具的测试


# ---- 知识库 / 向量库 / Embeddings / OCR ----


class FakeKnowledgeStore:
    """仿 OssStore 的内存子集(索引链路所需):list_all / get_object / object_exists /
    stat / delete_object。每个 key 带独立的 last_modified,可 bump 出"源文件被改动"。"""

    prefix = "knowledge/"

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.mtimes: dict[str, int] = {k: _FAKE_MTIME for k in self.files}

    def list_all(self, prefix: str = "") -> list[dict]:
        return [
            {"key": k, "size": len(v), "last_modified": self.mtimes.get(k, _FAKE_MTIME)}
            for k, v in sorted(self.files.items())
            if k.startswith(prefix)
        ]

    def get_object(self, key: str) -> bytes:
        return self.files[key]

    def object_exists(self, key: str) -> bool:
        return key in self.files

    def stat(self, key: str) -> dict | None:
        if key not in self.files:
            return None
        return {"size": len(self.files[key]), "last_modified": self.mtimes.get(key, _FAKE_MTIME)}

    def put_object(self, key: str, data: bytes, *, content_type=None, meta=None) -> str:
        self.files[key] = data
        self.mtimes[key] = _FAKE_MTIME
        return self.prefix + key

    def delete_object(self, key: str) -> None:
        self.files.pop(key, None)
        self.mtimes.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        """递归删相对前缀下的全部对象,返回删除个数(对齐 OssStore.delete_prefix)。"""
        victims = [k for k in self.files if k.startswith(prefix)]
        for k in victims:
            self.delete_object(k)
        return len(victims)

    def sign_url(self, key: str, expires: int = 900, method: str = "GET",
                 filename: str | None = None) -> str:
        """签名 URL 的离线替身(不真签名):把关键参数带进串里,供下载接口用例断言。"""
        q = f"expires={expires}"
        if filename:
            q += f"&disposition=attachment;filename*={filename}"
        return f"https://oss.test/{self.prefix}{key}?{q}"

    def touch(self, key: str, mtime: int | None = None) -> None:
        """模拟源文件被改写(mtime 变化 = 发布前应当拒绝)。"""
        self.mtimes[key] = (mtime if mtime is not None else self.mtimes.get(key, _FAKE_MTIME) + 1)


class FakeEmbeddings:
    """确定性假向量:同样的文本 → 同样的向量,维度对齐配置,便于断言集合维度与检索。"""

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.batches = 0
        self.texts: list[str] = []   # 实际提交过的文本,供断言"没重复付费调用"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.batches += 1
        self.texts.extend(texts)
        out = []
        for t in texts:
            seed = zlib.crc32(t.encode("utf-8"))
            out.append([((seed >> (i % 16)) % 97) / 97.0 for i in range(self.dim)])
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


@pytest.fixture
def cache_env():
    """隔离的进程内缓存:每个测试一份,测完卸载(不让假存储泄漏到别的测试)。"""
    from app.rag import cache_store

    cache = cache_store.MemoryCache()
    cache_store.install(cache)
    yield cache
    cache_store.install(None)


@pytest.fixture
def kb_env(cache_env, monkeypatch, tmp_path):
    """离线环境:内存知识库 + 内存 Qdrant + 假 Embeddings + 内存缓存 + 临时本地目录。

    返回一个 SimpleNamespace,可按需取用 kb / embeddings / qdrant / cache / cache_dir。
    """
    from types import SimpleNamespace

    from qdrant_client import QdrantClient

    from app.config import get_settings
    from app.rag import ingest, oss, store

    s = get_settings()
    monkeypatch.setattr(s, "index_state_dir", str(tmp_path / "ocr"))
    monkeypatch.setattr(s, "ocr_engine", "aliyun")
    monkeypatch.setattr(s, "ocr_aliyun_access_key_id", "fake-ak")
    monkeypatch.setattr(s, "ocr_aliyun_access_key_secret", "fake-sk")
    monkeypatch.setattr(s, "ocr_render_dpi", 72)  # 测试用小图,渲染更快

    kb = FakeKnowledgeStore()
    client = QdrantClient(location=":memory:")
    embeddings = FakeEmbeddings(s.embeddings_dim)

    monkeypatch.setattr(oss, "knowledge_store", lambda: kb)
    monkeypatch.setattr(store, "get_client", lambda: client)
    monkeypatch.setattr(ingest, "get_embeddings", lambda: embeddings)

    return SimpleNamespace(kb=kb, qdrant=client, embeddings=embeddings, cache=cache_env,
                           cache_dir=tmp_path / "ocr", settings=s)


@pytest.fixture(autouse=True)
def _no_real_libreoffice(monkeypatch):
    """.doc 转换默认离线:一律视为「本机没装 soffice」,任何测试都不真起 LibreOffice。

    与「OCR 不调真实供应商」同一条原则:真转换只留 test_doc_convert.py 末尾一条,
    它在采集期取真路径、装了才跑。
    """
    from app.rag import doc_convert

    monkeypatch.setattr(doc_convert, "soffice_bin", lambda: "")


# ---- 调用日志与费用统计 ----


class NoThreadWriter:
    """计量写入器的测试替身:入队照旧,但不启后台线程。

    真实写入器会起线程把队列排空,断言时会和测试抢时序;这里保留入队 / 补写 / 幂等
    这些真正要测的代码路径(MeteringWriter 本体),只把「什么时候冲刷」交给测试显式调用。
    """


@pytest.fixture
def metering_env(monkeypatch, tmp_path):
    """计量环境:内存仓库 + 不入队的后台线程模型 + 临时补写目录。

    MYSQL_URL 指向本机一个**不存在**的端口:测试永远不会真的去连它
    (仓库已换成内存实现),只用来让 db.configured() 为真、计量开关打开。
    """
    from types import SimpleNamespace

    from app.config import get_settings
    from app.metering import db, install_store, install_writer
    from app.metering.store import MemoryStore
    from app.metering.writer import MeteringWriter

    class _Writer(MeteringWriter):
        def _ensure_thread(self) -> None:
            return

        def start(self) -> None:      # 后台线程不启动:队列由测试自己 flush
            return

    s = get_settings()
    monkeypatch.setattr(s, "metering_enabled", True)
    monkeypatch.setattr(s, "mysql_url",
                        "mysql+pymysql://metering:never-used@127.0.0.1:1/qa_agent_test")
    monkeypatch.setattr(s, "metering_pending_dir", str(tmp_path / "pending"))
    monkeypatch.setattr(s, "metering_flush_batch", 200)

    memory = MemoryStore()
    install_store(memory)
    writer = _Writer()
    install_writer(writer)
    yield SimpleNamespace(store=memory, writer=writer, settings=s, pending=tmp_path / "pending")
    install_writer(None)
    install_store(None)
    db.dispose()


# ---- 认证 / 鉴权 ----
#
# 测试缝有两条,各自回答不同的问题:
#
# 1. **autouse 的 StubAuthService**:默认「已有一个管理员登录」。既有功能用例(计量 /
#    索引 / SOP / 会话……)要测的是功能本身,不该被逐条加上登录仪式;而鉴权正确性不靠
#    这些用例保证 —— 它由下面第 2 条的**真服务**与 test_auth_rbac.py 的**结构用例**
#    (遍历路由表,漏挂依赖就失败)来守。
# 2. **auth_env / auth_client 夹具**:真 AuthService + 真 SQL(临时 SQLite 文件)+
#    假发信 + 内存限流。认证接口 / 状态机 / 令牌轮换这些用例走这条,断言的是真代码。
#
# MySQL 专有的行为(行锁 FOR UPDATE、COLLATE utf8mb4_bin 的精确匹配、真列宽)不在 SQLite
# 上假装通过:见 tests/test_auth_mysql.py(需 AUTH_TEST_MYSQL_URL,否则跳过;
# 与 tests/test_metering_mysql.py 的 METERING_TEST_MYSQL_URL 是同一种约定)。

STUB_ADMIN_ID = "00000000-0000-4000-8000-00000000ad11"


def stub_principal(*, user_id: str = STUB_ADMIN_ID, role: str = "admin",
                   email: str = "stub-admin@example.com", status: str = "active",
                   display_name: str = "桩管理员"):
    """造一个 Principal(结构用例与替换用的桩都从这里取,避免各处手搓字段)。"""
    from app.auth.service import Principal

    return Principal(user_id=user_id, email=email, display_name=display_name, role=role,
                     status=status, auth_version=1, session_id="stub-session")


class StubAuthService:
    """认证服务桩:只回答「这个令牌是谁」,一律回答「上面那个管理员」。

    别的属性访问会直接炸(AttributeError)—— 认证路由若被非认证用例误触,应当大声失败,
    而不是拿到一个静默的假结果。
    """

    def __init__(self, principal=None) -> None:
        self.principal = principal or stub_principal()

    async def load_principal(self, *, user_id: str, session_id: str):
        return self.principal


_REAL_AUTH_FIXTURES = {"auth_env", "auth_client"}


@pytest.fixture(autouse=True)
def _stub_auth(request, monkeypatch):
    """默认假装「已经有一个管理员登录」(令牌校验这两步一并替换掉)。

    用真认证的用例(auth_client / auth_env)会**跳过**这一步:那类用例要断言真实的
    401 / 403 / 令牌轮换,必须走真服务与真签名校验。
    """
    if _REAL_AUTH_FIXTURES & set(request.fixturenames):
        yield None
        return

    from app.auth import deps
    from app.main import app

    stub = StubAuthService()
    monkeypatch.setattr(deps, "bearer_token", lambda authorization: "stub-access-token")
    monkeypatch.setattr(deps, "decode_access_token", lambda token: {
        "sub": stub.principal.user_id, "sid": stub.principal.session_id,
        "ver": stub.principal.auth_version,
    })
    # 配置自检(auth_ready)也要一并假装就位:功能用例不该被「本机没配 AUTH_JWT_SECRET /
    # 没连计量库」卡在 503 上 —— 那是运维配置,不是这些用例要测的东西。
    # 注意这里替换的是**模块属性**(auth_ready 在函数体里现取),不是提前导入的名字。
    from app.auth import db as auth_db
    from app.auth import tokens
    monkeypatch.setattr(auth_db, "configured", lambda: True)
    monkeypatch.setattr(tokens, "jwt_secret", lambda: "stub-only-secret")
    previous = getattr(app.state, "auth", None)
    app.state.auth = stub
    yield stub
    if previous is None:
        app.state.__delattr__("auth")
    else:
        app.state.auth = previous


def register_sqlite_collations(engine) -> None:
    """给 SQLite 注册 utf8mb4_bin(**真正的**二进制比较)。

    模型里 users.email 显式 COLLATE utf8mb4_bin(免得库里默认的不区分重音排序规则把
    a.b@x.com 与 a.b@x.com 之外的变体判成同一个地址)。SQLite 不认这个名字,建表会直接
    报 "no such collation sequence";这里补一个等价的实现。

    为什么是等价的:Python 的 str 比较按码点,而 UTF-8 的字节序与码点序一致 —— 所以
    「按码点比」就是「按字节比」。这样 SQLite 上的唯一键冲突用例仍然是真的在测
    「大小写 / 重音不同就是不同地址」,而不是把这条语义悄悄跳过去。
    """
    from sqlalchemy import event

    def _binary(a: str, b: str) -> int:
        return (a > b) - (a < b)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):  # noqa: ANN001
        dbapi_connection.create_collation("utf8mb4_bin", _binary)


class Clock:
    """可拨动的时钟:令牌过期 / 会话到期这类断言不该真的 sleep。"""

    def __init__(self) -> None:
        from datetime import datetime, timezone

        self.now = datetime.now(timezone.utc).replace(tzinfo=None)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        from datetime import timedelta

        self.now = self.now + timedelta(**kwargs)
        return self.now


@pytest.fixture
def auth_db(monkeypatch, tmp_path):
    """只要**真库**(临时 SQLite + 与迁移等价的真表),不要真登录。

    给「需要真表、但身份用什么无所谓」的用例(如会话目录):它们继续用 autouse 的
    「已登录管理员」桩,只把库换成真的。**刻意不放进 _REAL_AUTH_FIXTURES** ——
    放了的话桩会跳过,这些用例就得先真登录一遍,白白绕远。

    注意这里是 create_all 建表,不是跑迁移:迁移脚本本身由
    tests/test_auth_migration.py / test_metering_migration.py 与手写 SQL 逐句比对看护。
    """
    from types import SimpleNamespace

    from app.auth import db
    from app.auth.tables import Base
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "mysql_url",
                        f"sqlite+pysqlite:///{(tmp_path / 'auth.db').as_posix()}")
    db.dispose()                                   # 别让别的用例的引擎跨到这里
    engine = db.get_engine()
    register_sqlite_collations(engine)             # users.email 的 utf8mb4_bin
    Base.metadata.create_all(engine)
    yield SimpleNamespace(db=db, settings=s)
    db.dispose()


@pytest.fixture
def memory_db(monkeypatch, tmp_path):
    """长期记忆的真库（临时 SQLite + 与迁移等价的真表），身份用 autouse 的「已登录管理员」桩。

    与 auth_db 同一套：这里是 create_all 建表（仅测试），迁移脚本本身由
    tests/test_memory_migration.py 与手写 SQL 逐项比对看护。

    注意 MySQL 专有行为不在 SQLite 上假装通过：并发认领的行锁 / 真列宽另见
    tests/test_memory_mysql.py（需 MEMORY_TEST_MYSQL_URL，否则整体跳过）。
    """
    from types import SimpleNamespace

    from app.config import get_settings
    from app.memory import db
    from app.memory.tables import Base

    s = get_settings()
    monkeypatch.setattr(s, "mysql_url",
                        f"sqlite+pysqlite:///{(tmp_path / 'memory.db').as_posix()}")
    monkeypatch.setattr(s, "memory_enabled", True)
    db.dispose()                                   # 别让别的用例的引擎跨到这里
    engine = db.get_engine()
    Base.metadata.create_all(engine)
    yield SimpleNamespace(db=db, settings=s)
    db.dispose()


@pytest.fixture
def auth_env(auth_db, monkeypatch):
    """真认证环境:真 AuthService + SQLite 真表(auth_db)+ 假发信 + 内存限流。

    返回 SimpleNamespace(service, mailer, limiter, clock, settings, db)。
    """
    from types import SimpleNamespace

    from app.auth import mailer, ratelimit
    from app.auth.ratelimit import MemoryLimiter
    from app.auth.service import AuthService

    s = auth_db.settings
    # 密钥够强(>=32 字节)且是测试专用串;cookie 走 http://testserver,Secure 必须关。
    monkeypatch.setattr(s, "auth_jwt_secret", "test-only-secret-" + "0" * 32)
    monkeypatch.setattr(s, "auth_cookie_secure", False)
    monkeypatch.setattr(s, "auth_cookie_domain", "")
    monkeypatch.setattr(s, "auth_rate_limit_enabled", True)
    monkeypatch.setattr(s, "auth_frontend_base_url", "http://front.test")

    clock = Clock()
    fake_mailer = mailer.FakeMailer()
    limiter = MemoryLimiter()
    mailer.install_mailer(fake_mailer)
    ratelimit.install(limiter)

    service = AuthService(now=clock)
    env = SimpleNamespace(service=service, mailer=fake_mailer, limiter=limiter, clock=clock,
                          settings=s, db=auth_db.db)
    yield env
    mailer.install_mailer(None)
    ratelimit.install(None)


@pytest.fixture
def auth_client(auth_env):
    """挂上真认证服务的 TestClient(不跑 lifespan:不连 Redis、不起后台线程)。"""
    from fastapi.testclient import TestClient

    from app.main import app

    app.state.auth = auth_env.service
    return TestClient(app)
