"""记忆向量索引层(§4/§7):集合形态 / 版本化点 id / 作用域隔离 / 删除 / 重建。

用 qdrant-client 自带的内存实例(真 API、零外部依赖):这里测的是我们这层的约定,
不是 Qdrant 本身。

重点看护「旧索引覆盖新索引」这一族问题:
- 点 id 编入正文版本 → 不同版本是不同的点,旧写入**不可能**覆盖新点;
- 检索按 embedding_version + scope + user_id 过滤 → 旧模型 / 别的作用域的点不是候选;
- delete_older_revisions 只删比当前版本更旧的点 → 并发写新版不会被误删;
- delete_user 带 generation 上界 → 清除期间新写的记忆不受影响。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient

from app.config import get_settings
from app.memory import vector
from app.memory.errors import MemoryConflict, MemoryNotConfigured
from app.memory.models import MemoryItem, content_hash


@pytest.fixture
def vec_env(monkeypatch):
    """内存 Qdrant:接管 rag.store.get_client(记忆层经它复用同一个客户端)。"""
    from app.rag import store

    client = QdrantClient(location=":memory:")
    monkeypatch.setattr(store, "get_client", lambda: client)
    return SimpleNamespace(client=client, settings=get_settings())


def _item(memory_id: str, user_id: str, text: str, *, revision: int = 1, scope: str = "",
          meta_version: int = 1, generation: int = 0, embedding_version: str = "v1",
          kind: str = "preference") -> MemoryItem:
    now = datetime(2026, 10, 1, 12, 0, 0)

    return MemoryItem(
        id=memory_id, user_id=user_id, text=text, content_hash=content_hash(text),
        status="active", origin="llm", thread_id=None, revision=revision,
        embedding_version=embedding_version, indexed_at=None, created_at=now,
        updated_at=now, deleted_at=None, scope=scope, kind=kind, generation=generation,
        meta_version=meta_version,
    )


def _unit_vec(seed: int, dim: int) -> list[float]:
    """确定性单位向量(不同 seed 之间近似正交,便于断言归属)。"""
    v = [0.0] * dim
    v[seed % dim] = 1.0
    return v


def _point(memory_id: str, dim: int, *, revision: int = 1):
    return vector.point_id(memory_id, revision), dim


# ---- 集合 ----


def test_ensure_collection_creates_with_configured_dim(vec_env):
    name = vector.ensure_collection()

    assert name == vec_env.settings.memory_collection
    assert vec_env.client.collection_exists(name)
    assert vector.collection_dim() == vec_env.settings.embeddings_dim
    assert vector.ensure_collection() == name                      # 幂等
    assert vector.count() == 0


def test_ensure_collection_declares_sparse_channel(vec_env):
    """集合必须同时建好稀疏通道:少了它关键词通道会静默空着(不报错)。"""
    vector.ensure_collection()

    state = vector.collection_state()

    assert state["exists"] and state["sparse"] is True
    assert state["compatible"] is True and state["reason"] == ""


def test_missing_collection_reads_as_empty_not_error(vec_env):
    """集合还没建时:读路径返回空/None,不抛错(写入路径才建集合)。

    稀疏通道这里刻意区分 `None`(通道不可用)与 `[]`(通道可用但没命中):调用方要
    据此决定是降级到进程内 BM25,还是把「没命中」当成正常结果。
    """
    assert vector.collection_dim() is None
    assert vector.collection_state()["exists"] is False
    assert vector.count() == 0
    assert vector.search([0.0] * vec_env.settings.embeddings_dim, user_id="u1",
                         scope="", top_k=5) == []
    assert vector.search_sparse("偏好", user_id="u1", scope="", top_k=5) is None
    assert vector.delete_ids(["a" * 32]) == 0                      # 没有可删的
    assert vector.delete_user("u1", scope="") == 0
    assert vector.refresh_payload("a" * 36, {"revision": 2}) is False


def test_point_id_is_stable_and_versioned(vec_env):
    """点 id = f(记忆, 正文版本):稳定(重建可复现)且不同版本不同点。"""
    mid = "0123456789abcdef0123456789abcdef"

    pid1 = vector.point_id(mid, 1)

    assert pid1 == vector.point_id(mid, 1)                          # 稳定
    assert pid1 != vector.point_id(mid, 2)                          # 版本进 id
    assert pid1 != vector.point_id("f" * 32, 1)                     # 记忆进 id


def test_client_without_qdrant_url_raises_memory_not_configured(vec_env, monkeypatch):
    """未配置 QDRANT_URL:报「记忆索引不可用」,不冒充「你没有记忆」。"""
    from app.rag import store

    def boom():
        raise RuntimeError("未配置 QDRANT_URL")

    monkeypatch.setattr(store, "get_client", boom)
    with pytest.raises(MemoryNotConfigured):
        vector.ensure_collection()


# ---- 写入 / 召回 ----


def test_upsert_and_search_are_isolated_by_user(vec_env):
    dim = vec_env.settings.embeddings_dim
    a, b = "a" * 32, "b" * 32
    vector.upsert([vector.make_point(_item(a, "u1", "用户偏好中文"), _unit_vec(0, dim)),
                   vector.make_point(_item(b, "u2", "用户偏好英文"), _unit_vec(0, dim))])

    hits = vector.search(_unit_vec(0, dim), user_id="u1", scope="", top_k=5)

    assert [vector.id_of(p) for p in hits] == [a]                  # 别人的记忆召不回来
    assert vector.count() == 2 and vector.count(user_id="u1") == 1


def test_search_is_isolated_by_scope(vec_env):
    """同一个人两个作用域(eval:xxx / 正式)互不可见 —— 评测数据不许混进正式召回。"""
    dim = vec_env.settings.embeddings_dim
    a, b = "a" * 32, "b" * 32
    vector.upsert([vector.make_point(_item(a, "u1", "正式记忆"), _unit_vec(0, dim)),
                   vector.make_point(_item(b, "u1", "评测记忆", scope="eval:r1"),
                                     _unit_vec(0, dim))])

    formal = vector.search(_unit_vec(0, dim), user_id="u1", scope="", top_k=5)
    ev = vector.search(_unit_vec(0, dim), user_id="u1", scope="eval:r1", top_k=5)

    assert [vector.id_of(p) for p in formal] == [a]
    assert [vector.id_of(p) for p in ev] == [b]


def test_search_filters_out_other_embedding_versions(vec_env):
    """旧模型口径的点不是候选:换模型后即使索引没重建完,也不会用旧向量按新口径打分。"""
    dim = vec_env.settings.embeddings_dim
    a, b = "a" * 32, "b" * 32
    vector.upsert([vector.make_point(_item(a, "u1", "旧模型", embedding_version="v1"),
                                     _unit_vec(0, dim)),
                   vector.make_point(_item(b, "u1", "新模型", embedding_version="v2"),
                                     _unit_vec(0, dim))])

    hits = vector.search(_unit_vec(0, dim), user_id="u1", scope="", top_k=5,
                         embedding_version="v2")

    assert [vector.id_of(p) for p in hits] == [b]


def test_payload_has_no_body_text_and_carries_version_fields(vec_env):
    dim = vec_env.settings.embeddings_dim
    mid = "c" * 32
    vector.upsert([vector.make_point(
        _item(mid, "u1", "用户的银行卡尾号不方便说", revision=3, meta_version=2,
              generation=5), _unit_vec(1, dim))])

    point = vec_env.client.retrieve(collection_name=vector.collection_name(),
                                    ids=[vector.point_id(mid, 3)])[0]

    assert set(point.payload) == set(vector.PAYLOAD_FIELDS)
    assert point.payload["memory_id"] == mid and point.payload["user_id"] == "u1"
    assert point.payload["revision"] == 3 and point.payload["meta_version"] == 2
    assert point.payload["generation"] == 5 and point.payload["content_hash"]
    assert point.payload["kind"] == "preference"
    assert "银行卡" not in str(point.payload)                       # 正文只在 MySQL


def test_id_of_returns_empty_when_payload_has_no_memory_id():
    """payload 缺 memory_id 时宁可丢弃,不从点 id 反推(带版本的点 id 反推不出记忆 id)。"""
    from qdrant_client.models import ScoredPoint

    point = ScoredPoint(id=vector.point_id("d" * 32, 1), version=0, score=0.5, payload={})

    assert vector.id_of(point) == ""


def test_new_revision_writes_a_new_point_and_never_overwrites_the_old(vec_env):
    """旧执行者迟到写回旧版本:它落在**另一个点**上,新版本的点纹丝不动。"""
    dim = vec_env.settings.embeddings_dim
    mid = "e" * 32
    vector.upsert([vector.make_point(_item(mid, "u1", "新正文", revision=2), _unit_vec(2, dim))])
    vector.upsert([vector.make_point(_item(mid, "u1", "旧正文", revision=1), _unit_vec(0, dim))])

    name = vector.collection_name()
    new = vec_env.client.retrieve(collection_name=name, ids=[vector.point_id(mid, 2)])[0]
    old = vec_env.client.retrieve(collection_name=name, ids=[vector.point_id(mid, 1)])[0]

    assert new.payload["revision"] == 2 and old.payload["revision"] == 1
    assert vector.count() == 2                                     # 两个版本各占一个点


def test_sparse_channel_recalls_by_keyword(vec_env):
    """稀疏通道按词项召回(与稠密通道独立):查询词命中哪条,就召回哪条。"""
    dim = vec_env.settings.embeddings_dim
    a, b = "a" * 32, "b" * 32
    vector.upsert([vector.make_point(_item(a, "u1", "报销单需要贴发票原件"), _unit_vec(0, dim)),
                   vector.make_point(_item(b, "u1", "实验室门禁卡要每周检查"), _unit_vec(1, dim))])

    hits = vector.search_sparse("发票", user_id="u1", scope="", top_k=5)

    assert [vector.id_of(p) for p in hits] == [a]
    assert vector.search_sparse("完全无关的词", user_id="u1", scope="", top_k=5) == []


def test_sparse_channel_filters_by_scope_and_version(vec_env):
    dim = vec_env.settings.embeddings_dim
    a, b = "a" * 32, "b" * 32
    vector.upsert([vector.make_point(_item(a, "u1", "正式入库流程"),
                                     _unit_vec(0, dim), ),
                   vector.make_point(_item(b, "u1", "评测入库流程", scope="eval:r1",
                                           embedding_version="v2"), _unit_vec(0, dim))])

    assert vector.search_sparse("入库", user_id="u1", scope="", top_k=5,
                                embedding_version="v1") != []
    assert vector.search_sparse("入库", user_id="u1", scope="eval:r1", top_k=5,
                                embedding_version="v2") != []
    assert vector.search_sparse("入库", user_id="u1", scope="", top_k=5,
                                embedding_version="v2") == []


def test_refresh_payload_updates_metadata_without_touching_vector(vec_env):
    """元数据变更:点还在就只刷 payload(正文没变,没必要重新向量化)。"""
    dim = vec_env.settings.embeddings_dim
    mid = "f" * 32
    vector.upsert([vector.make_point(_item(mid, "u1", "正文没变"), _unit_vec(0, dim))])

    ok = vector.refresh_payload(vector.point_id(mid, 1),
                                {"memory_id": mid, "user_id": "u1", "revision": 1,
                                 "meta_version": 2})

    assert ok is True
    point = vec_env.client.retrieve(collection_name=vector.collection_name(),
                                    ids=[vector.point_id(mid, 1)])[0]
    assert point.payload["meta_version"] == 2
    assert vector.count() == 1


def test_refresh_payload_reports_missing_point(vec_env):
    """点不存在(集合被重建 / 被别人删掉)必须返回 False:调用方据此重新向量化,而不是
    把「payload 刷了」当成「索引最新」——那会留下一条永远检索不到的已索引记忆。"""
    vector.ensure_collection()

    assert vector.refresh_payload("0" * 36, {"revision": 1}) is False


def test_points_exist_reports_only_present_points(vec_env):
    dim = vec_env.settings.embeddings_dim
    mid = "a" * 32
    vector.upsert([vector.make_point(_item(mid, "u1", "甲"), _unit_vec(0, dim))])

    got = vector.points_exist([vector.point_id(mid, 1), vector.point_id(mid, 2)])

    assert got == {vector.point_id(mid, 1)}


# ---- 删除 ----


def test_delete_ids_removes_all_revisions_of_a_memory(vec_env):
    """删一条记忆要连它的历史版本一起清掉(按 payload.memory_id 过滤,不是按点 id)。"""
    dim = vec_env.settings.embeddings_dim
    a, b = "a" * 32, "b" * 32
    vector.upsert([vector.make_point(_item(a, "u1", "旧正文", revision=1), _unit_vec(0, dim)),
                   vector.make_point(_item(a, "u1", "新正文", revision=2), _unit_vec(1, dim)),
                   vector.make_point(_item(b, "u1", "别人的"), _unit_vec(2, dim))])

    assert vector.delete_ids([a]) == 1
    assert vector.count() == 1
    assert vector.delete_ids([a]) == 1                             # 幂等:重复删不报错
    assert vector.count() == 1


def test_delete_older_revisions_keeps_current_and_newer(vec_env):
    dim = vec_env.settings.embeddings_dim
    mid = "a" * 32
    vector.upsert([vector.make_point(_item(mid, "u1", "v1", revision=1), _unit_vec(0, dim)),
                   vector.make_point(_item(mid, "u1", "v2", revision=2), _unit_vec(0, dim)),
                   vector.make_point(_item(mid, "u1", "v3", revision=3), _unit_vec(0, dim))])

    assert vector.delete_older_revisions(mid, revision=2, scope="") == 1

    name = vector.collection_name()
    left = {vector.point_id(mid, 2), vector.point_id(mid, 3)}
    assert vector.points_exist(left) == left
    assert vector.points_exist([vector.point_id(mid, 1)]) == set()


def test_delete_user_respects_scope_and_generation(vec_env):
    """彻底清除:同一用户的其他作用域、以及清除之后新写(代次更高)的点都留着。"""
    dim = vec_env.settings.embeddings_dim
    old_gen = _item("a" * 32, "u1", "清除前的记忆", generation=0)
    new_gen = _item("b" * 32, "u1", "清除后新写的记忆", generation=1)
    other_scope = _item("c" * 32, "u1", "评测数据", scope="eval:r1", generation=0)
    vector.upsert([vector.make_point(old_gen, _unit_vec(0, dim)),
                   vector.make_point(new_gen, _unit_vec(1, dim)),
                   vector.make_point(other_scope, _unit_vec(2, dim))])

    n = vector.delete_user("u1", scope="", before_generation=1)

    assert n == 1                                                  # 只删了清除前那一个点
    left = vector.points_exist([vector.point_id("b" * 32, 1), vector.point_id("c" * 32, 1)])
    assert left == {vector.point_id("b" * 32, 1), vector.point_id("c" * 32, 1)}


def test_delete_user_without_generation_bound_removes_all_in_scope(vec_env):
    dim = vec_env.settings.embeddings_dim
    vector.upsert([vector.make_point(_item("a" * 32, "u1", "甲"), _unit_vec(0, dim)),
                   vector.make_point(_item("b" * 32, "u2", "乙"), _unit_vec(1, dim))])

    assert vector.delete_user("u1", scope="") == 1

    assert vector.count() == 1 and vector.count(user_id="u2") == 1
    assert vector.delete_user("u1", scope="") == 0                 # 幂等


# ---- 维度变更:显式重建,不自动删集合 ----


def test_assert_dimension_raises_on_mismatch(vec_env, monkeypatch):
    monkeypatch.setattr(vec_env.settings, "embeddings_dim", 8)
    vector.ensure_collection()

    vector.assert_dimension()                                      # 一致:放行

    monkeypatch.setattr(vec_env.settings, "embeddings_dim", 4)
    with pytest.raises(MemoryConflict) as ei:
        vector.assert_dimension()
    assert "重建" in str(ei.value)                                  # 报错要给出路


def test_assert_dimension_raises_when_sparse_channel_missing(vec_env, monkeypatch, tmp_path):
    """集合少建了稀疏通道也必须拦住(否则关键词通道一直空着,还不报错)。"""
    from qdrant_client.models import Distance, VectorParams

    name = vector.collection_name()
    vec_env.client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=int(vec_env.settings.embeddings_dim),
                                    distance=Distance.COSINE))

    assert vector.collection_state()["sparse"] is False
    with pytest.raises(MemoryConflict) as ei:
        vector.assert_dimension()
    assert "重建" in str(ei.value)


def test_recreate_collection_switches_dim_and_drops_points(vec_env, monkeypatch):
    monkeypatch.setattr(vec_env.settings, "embeddings_dim", 8)
    vector.upsert([vector.make_point(_item("a" * 32, "u1", "甲"), _unit_vec(0, 8))])
    assert vector.count() == 1

    monkeypatch.setattr(vec_env.settings, "embeddings_dim", 4)
    vector.recreate_collection()

    assert vector.collection_dim() == 4
    assert vector.count() == 0                                     # 索引清空,正文仍在 MySQL
    assert vector.collection_state()["compatible"] is True
