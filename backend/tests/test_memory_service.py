"""记忆写入与索引编排:先落库、再索引、失败留待重放、版本与 revision 的竞态防护。

真表(临时 SQLite,与迁移等价)+ 内存 Qdrant + 假 Embeddings + 内存缓存 —— 全程离线。
MySQL 专有行为(行锁认领 / 真列宽)不在这里假装通过,见 tests/test_memory_mysql.py。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient

from app.config import get_settings
from app.memory import repo, service, vector
from app.memory.errors import MemoryConflict
from app.memory.extract import ExtractedFact
from app.memory.models import ACTOR_USER, EVENT_ADD, EVENT_UPDATE, ORIGIN_LLM, SCOPE_FORMAL
from tests.conftest import FakeEmbeddings

U1 = "00000000-0000-4000-8000-000000000001"
U2 = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def mem_env(memory_db, cache_env, monkeypatch):
    """离线记忆环境:真表 + 内存 Qdrant + 假 Embeddings + 内存缓存。"""
    from app.rag import store

    s = memory_db.settings
    client = QdrantClient(location=":memory:")
    embeddings = FakeEmbeddings(s.embeddings_dim)

    monkeypatch.setattr(store, "get_client", lambda: client)      # 记忆层经它复用客户端
    monkeypatch.setattr(service, "get_embeddings", lambda: embeddings)

    return SimpleNamespace(db=memory_db.db, repo=repo, settings=s, qdrant=client,
                           embeddings=embeddings, cache=cache_env, dim=s.embeddings_dim)


def _fact(text: str, kind: str = "preference") -> ExtractedFact:
    return ExtractedFact(text=text, kind=kind)


def _row(env, user_id: str, memory_id: str):
    with env.db.session_scope() as session:
        return env.repo.get_item(session, user_id, memory_id)


def _history(env, user_id: str, memory_id: str | None = None):
    with env.db.session_scope() as session:
        return env.repo.list_history(session, user_id, memory_id=memory_id)


# ---- 写入:事实落库 + 索引 ----


def test_write_facts_persists_audits_and_indexes(mem_env):
    text = "用户偏好用中文写实验记录"
    result = service.write_facts(user_id=U1, facts=[_fact(text)], thread_id="t1")

    assert len(result.outcome.added) == 1
    item = result.outcome.added[0]
    assert result.index.requested == 1 and result.index.indexed == 1
    assert result.index.deferred == 0 and not result.index.error
    assert result.index.indexed <= result.index.requested           # 标记数不可能超过请求数

    row = _row(mem_env, U1, item.id)                               # MySQL 是事实源
    assert row is not None and row.text == text
    assert row.status == "active" and row.origin == ORIGIN_LLM and row.revision == 1
    assert row.embedding_version == service.embedding_version()
    assert row.indexed_at is not None

    history = _history(mem_env, U1, item.id)
    assert [h.event for h in history] == [EVENT_ADD]
    assert history[0].new_text == text and history[0].thread_id == "t1"
    assert history[0].actor == "llm"

    hits = vector.search(mem_env.embeddings.embed_query(text), user_id=U1,
                         scope=SCOPE_FORMAL, top_k=3)
    assert [vector.id_of(p) for p in hits] == [item.id]            # 索引里指向同一条记忆
    assert vector.count(user_id=U2) == 0


def test_duplicate_facts_are_skipped_not_written_twice(mem_env):
    """按规范化正文去重:全角 / 半角这种写法差异算同一条;重复是正常结果,不报错。"""
    result = service.write_facts(user_id=U1, facts=[
        _fact("用户偏好把结果导出为ＣＳＶ"),                        # 全角
        _fact("用户偏好把结果导出为CSV"),                           # 半角 → NFKC 后同一条
        _fact("用户偏好用中文写实验记录"),                          # 新的一条
    ])

    assert len(result.outcome.added) == 2 and result.outcome.skipped == 1

    again = service.write_facts(user_id=U1, facts=[_fact("用户偏好把结果导出为CSV")])
    assert again.outcome.added == [] and again.outcome.skipped == 1

    with mem_env.db.session_scope() as session:
        assert mem_env.repo.count_items(session, U1) == 2
    assert vector.count(user_id=U1) == 2


def test_empty_facts_write_nothing(mem_env):
    result = service.write_facts(user_id=U1, facts=[])

    assert result.outcome.added == [] and result.index.requested == 0
    assert vector.count() == 0


# ---- 索引失败:只让行留待重放,绝不写坏事实 ----


def test_index_write_failure_defers_and_replay_reuses_cached_vectors(mem_env, monkeypatch):
    original = vector.upsert

    def boom(points):
        raise RuntimeError("qdrant 不可达")

    monkeypatch.setattr(vector, "upsert", boom)                    # 向量都算好了,只是写不进去
    text = "用户在材料学院做电池实验"
    result = service.write_facts(user_id=U1, facts=[_fact(text)])

    assert len(result.outcome.added) == 1                          # 事实照常落库
    assert result.index.deferred == 1 and result.index.indexed == 0
    assert "RuntimeError" in result.index.error

    item = result.outcome.added[0]
    row = _row(mem_env, U1, item.id)
    assert row is not None and row.text == text                    # 正文一个字都没丢
    assert row.embedding_version == "" and row.indexed_at is None  # 标脏:等重建
    assert vector.count() == 0
    paid = len(mem_env.embeddings.texts)                            # 这一轮真的付过费用的文本

    monkeypatch.setattr(vector, "upsert", original)                # 索引层恢复
    again = service.ensure_indexed(user_id=U1)

    assert again.requested == 1 and again.indexed == 1
    assert _row(mem_env, U1, item.id).embedding_version == service.embedding_version()
    assert vector.count(user_id=U1) == 1
    assert len(mem_env.embeddings.texts) == paid                   # 正文没变:走缓存,不再付费


def test_embedding_unavailable_defers_without_losing_the_fact(mem_env, monkeypatch):
    def boom():
        raise RuntimeError("embedding 端点不可达")

    monkeypatch.setattr(service, "get_embeddings", boom)
    result = service.write_facts(user_id=U1, facts=[_fact("用户在用校园网访问集群")])

    assert len(result.outcome.added) == 1
    assert result.index.deferred == 1 and vector.count() == 0
    assert _row(mem_env, U1, result.outcome.added[0].id).embedding_version == ""


def test_dimension_mismatch_defers_and_asks_for_explicit_rebuild(mem_env, monkeypatch):
    monkeypatch.setattr(mem_env.settings, "embeddings_dim", 8)
    vector.ensure_collection()                                     # 按旧维度建了集合
    monkeypatch.setattr(mem_env.settings, "embeddings_dim", mem_env.dim)

    result = service.write_facts(user_id=U1, facts=[_fact("用户偏好附上原始数据")])

    assert result.index.deferred == 1
    assert "MemoryConflict" in result.index.error                  # 报错要说清要显式重建
    assert result.index.indexed == 0 and vector.count() == 0       # 没有写坏集合


# ---- 重建:按版本补索引 / 按用户限定 ----


def test_ensure_indexed_noop_when_everything_is_current(mem_env):
    service.write_facts(user_id=U1, facts=[_fact("用户偏好中文回复")])

    result = service.ensure_indexed()

    assert result.requested == 0 and result.indexed == 0 and result.deferred == 0


def test_version_bump_marks_rows_pending_and_rebuilds(mem_env, monkeypatch):
    text = "用户每周五整理实验数据"
    service.write_facts(user_id=U1, facts=[_fact(text)])
    first_version = service.embedding_version()

    monkeypatch.setattr(mem_env.settings, "embeddings_version", f"{first_version}-next")
    new_version = service.embedding_version()
    assert new_version != first_version                            # 版本口径变了

    with mem_env.db.session_scope() as session:
        pending = mem_env.repo.pending_index_ids(session, embedding_version=new_version)
    assert len(pending) == 1                                       # 同一条行变「待重建」

    result = service.ensure_indexed()
    assert result.requested == 1 and result.indexed == 1
    with mem_env.db.session_scope() as session:
        row = mem_env.repo.list_items(session, U1)[0]
    assert row.embedding_version == new_version                    # 行上的版本跟着走
    assert text in mem_env.embeddings.texts                        # 按新版本重算过向量


def test_ensure_indexed_can_be_scoped_to_one_user(mem_env, monkeypatch):
    original = vector.upsert
    monkeypatch.setattr(vector, "upsert", lambda points: (_ for _ in ()).throw(RuntimeError("down")))
    service.write_facts(user_id=U1, facts=[_fact("用户偏好中文")])
    service.write_facts(user_id=U2, facts=[_fact("用户偏好英文")])
    monkeypatch.setattr(vector, "upsert", original)

    scoped = service.ensure_indexed(user_id=U1)

    assert scoped.requested == 1 and scoped.indexed == 1
    assert vector.count(user_id=U1) == 1 and vector.count(user_id=U2) == 0

    rest = service.ensure_indexed()                                # 不限用户:把剩下的补上
    assert rest.indexed == 1 and vector.count(user_id=U2) == 1


# ---- revision 竞态:旧向量不能把新正文标成「已索引」 ----


def test_mark_indexed_skips_rows_rewritten_midflight(mem_env):
    now = mem_env.db.utc_naive()
    with mem_env.db.session_scope() as session:
        item = mem_env.repo.create_item(session, user_id=U1, text="甲的偏好", now=now)
    stale_revision = item.revision

    with mem_env.db.session_scope() as session:                    # 「向量化期间」正文被改写
        mem_env.repo.update_item(session, user_id=U1, memory_id=item.id, new_text="乙的偏好",
                                 actor=ACTOR_USER, now=now)

    with mem_env.db.session_scope() as session:
        marked = mem_env.repo.mark_indexed(session, ids=[item.id], embedding_version="v1", now=now,
                                           revisions={item.id: stale_revision})
    assert marked == 0                                             # 旧向量不配标新正文

    row = _row(mem_env, U1, item.id)
    assert row.embedding_version == "" and row.indexed_at is None  # 仍待重建
    assert [h.event for h in _history(mem_env, U1, item.id)] == [EVENT_UPDATE, EVENT_ADD]

    with mem_env.db.session_scope() as session:                    # 用新 revision 再标 → 成功
        marked = mem_env.repo.mark_indexed(session, ids=[item.id], embedding_version="v1", now=now,
                                           revisions={item.id: row.revision})
    assert marked == 1
    assert _row(mem_env, U1, item.id).embedding_version == "v1"


def test_mark_indexed_ignores_deleted_rows(mem_env):
    now = mem_env.db.utc_naive()
    with mem_env.db.session_scope() as session:
        item = mem_env.repo.create_item(session, user_id=U1, text="待删除的偏好", now=now)
    with mem_env.db.session_scope() as session:
        mem_env.repo.delete_item(session, user_id=U1, memory_id=item.id, actor=ACTOR_USER, now=now)

    with mem_env.db.session_scope() as session:
        assert mem_env.repo.mark_indexed(session, ids=[item.id], embedding_version="v1",
                                         now=now) == 0
        assert mem_env.repo.pending_index_ids(session, embedding_version="v1") == []


# ---- 版本口径 ----


def test_embedding_version_tracks_model_dim_and_tokenizer(mem_env, monkeypatch):
    """口径由**代码**算出来:模型 / 版本号 / 维度 / 分词任一变化都要反映到版本串里。

    只靠人工记得 +1 EMBEDDINGS_VERSION 一定会漏掉「只改了向量维度」「只改了分词规则」——
    那时旧向量与新查询不在同一个空间,检索会静默变差,而版本串看起来毫无变化。
    """
    from app.memory import text as memory_text

    base = service.embedding_version()
    assert base == service.embedding_version()                     # 同口径下稳定
    assert 0 < len(base) <= 32                                     # 不撑破列宽(32)

    monkeypatch.setattr(mem_env.settings, "embeddings_model", "text-embedding-v4")
    assert service.embedding_version() != base                     # 换模型 → 换口径

    monkeypatch.setattr(mem_env.settings, "embeddings_model", "m" * 60)   # 超列宽(32)
    long_version = service.embedding_version()
    assert len(long_version) == 32                                 # 超长退化成定长摘要
    assert long_version == service.embedding_version()             # 仍然稳定可比

    monkeypatch.setattr(mem_env.settings, "embeddings_dim", int(mem_env.settings.embeddings_dim) + 1)
    assert service.embedding_version() != long_version             # 只改维度也要变

    monkeypatch.setattr(memory_text, "index_version", lambda: "tok:other-v1:k1=1.2")
    assert service.embedding_version() != long_version             # 只改分词口径也要变


def test_write_facts_uses_given_clock_and_reason(mem_env):
    """保存来源与时间:审计行要能回答「这条是怎么来的」。"""
    fixed = mem_env.db.utc_naive()
    result = service.write_facts(user_id=U1, facts=[_fact("用户在做报销流程优化", kind="task")],
                                 thread_id="t9", now=fixed, reason="会话提取(测试)")
    item = result.outcome.added[0]

    row = _row(mem_env, U1, item.id)
    assert row.created_at == fixed and row.updated_at == fixed
    history = _history(mem_env, U1, item.id)[0]
    assert history.created_at == fixed and history.thread_id == "t9"
    assert history.reason == "会话提取(测试)"


def test_a_metadata_only_change_does_not_re_embed(mem_env):
    """正文一个字没改、只动了元数据:只刷 payload,**一次 Embedding 都不发**。

    「靠缓存命中省下调用」是另一回事:这里断言的是业务层根本没发起这次调用 ——
    缓存命中与否不该被当成「没有重新向量化」的证据。
    """
    text = "用户每周五整理实验数据"
    item = service.write_facts(user_id=U1, facts=[_fact(text)]).outcome.added[0]
    mem_env.embeddings.texts.clear()                     # 只统计下面这一步

    with mem_env.db.session_scope() as session:          # 改类型(元数据),正文照旧
        updated = mem_env.repo.update_item(session, user_id=U1, memory_id=item.id,
                                           new_text=text, kind="task", actor=ACTOR_USER,
                                           now=mem_env.db.utc_naive(), reason="补类型")
    assert updated.revision == 1 and updated.meta_version == 1

    result = service.ensure_indexed(user_id=U1)

    assert result.payload_only == 1 and result.indexed == 0 and result.deferred == 0
    assert mem_env.embeddings.texts == []                 # 零次向量化调用
    payload = mem_env.qdrant.retrieve(
        collection_name=vector.collection_name(),
        ids=[vector.point_id(item.id, 1)], with_payload=True, with_vectors=False)[0].payload
    assert payload["kind"] == "task"                      # 新元数据真的写进了索引
    with mem_env.db.session_scope() as session:
        row = mem_env.repo.get_item(session, U1, item.id)
    assert row.index_synced(service.embedding_version())   # 三个版本口径一起对上了


def test_repair_is_the_only_path_that_catches_index_marks_that_lie(mem_env):
    """标记说「已同步」、点却不在集合里:对账能看出来,repair 能把索引补回去。

    这是唯一能自动发现这类损坏的路:待索引集本身由标记算出来,标记被写坏时它永远发现不了。
    """
    text = "用户偏好附上原始数据"
    service.write_facts(user_id=U1, facts=[_fact(text)])
    mem_env.qdrant.delete_collection(vector.collection_name())     # 集合被重建 / 点被清掉

    assert service.index_drift()["drift"] is True         # 对账:点数 0 < 记忆 1

    result = service.ensure_indexed(repair=True)          # 显式修复:整批重算标记

    assert result.repaired == 1 and result.indexed == 1
    assert service.index_drift()["drift"] is False
    assert vector.count() == 1 and text in mem_env.embeddings.texts


def test_memory_conflict_is_translated_not_raised(mem_env, monkeypatch):
    """repo 层的去重兜底抛 MemoryConflict 时,service 记 skipped(正常去重,不是失败)。"""
    with mem_env.db.session_scope() as session:
        created = mem_env.repo.create_item(session, user_id=U1, text="并发写入的同一条",
                                           now=mem_env.db.utc_naive())
    assert created.revision == 1

    with pytest.raises(MemoryConflict):
        with mem_env.db.session_scope() as session:
            mem_env.repo.create_item(session, user_id=U1, text="并发写入的同一条",
                                     now=mem_env.db.utc_naive())

    result = service.write_facts(user_id=U1, facts=[_fact("并发写入的同一条")])
    assert result.outcome.skipped == 1 and result.outcome.added == []


def test_settings_snapshot_matches_module_defaults():
    """夹具用的是真实配置(不是手搓常量):维度 / 集合名都从 settings 来。"""
    s = get_settings()
    assert s.embeddings_dim > 0 and s.memory_collection
    assert vector.collection_name() == s.memory_collection
