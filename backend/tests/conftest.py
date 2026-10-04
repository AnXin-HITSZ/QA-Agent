"""测试夹具:OSS 读写、Qdrant、Embeddings、OCR 全部换成离线假实现。

- SOP:接管 app.rag.oss.sops_store(内存假仓库,不触网);
- 知识库:接管 app.rag.oss.knowledge_store(内存假仓库,可改 mtime 模拟源文件变化);
- 向量库:QdrantClient(":memory:")(qdrant-client 自带,真实 API、零外部依赖);
- Embeddings:确定性假向量(维度对齐 EMBEDDINGS_DIM,便于校验集合维度);
- 缓存:进程内 MemoryCache(方案要求的「隔离的测试存储」),不碰真实 Redis;
- OCR:任何测试都不调用真实供应商 —— monkeypatch ocr.recognize_raw(网络边界,
  返回假响应 dict),两层缓存 / 归一化仍真实执行;识别结果全部由测试给定。
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


# ---- 调用日志与费用统计 ----


class NoThreadWriter:
    """计量写入器的测试替身:入队照旧,但不启后台线程。

    真实写入器会起线程把队列排空,断言时会和测试抢时序;这里保留入队 / 补写 / 幂等
    这些真正要测的代码路径(MeteringWriter 本体),只把「什么时候冲刷」交给测试显式调用。
    """


@pytest.fixture
def metering_env(monkeypatch, tmp_path):
    """计量环境:内存仓库 + 不入队的后台线程模型 + 临时补写目录。

    METERING_MYSQL_URL 指向本机一个**不存在**的端口:测试永远不会真的去连它
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
    monkeypatch.setattr(s, "metering_mysql_url",
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
