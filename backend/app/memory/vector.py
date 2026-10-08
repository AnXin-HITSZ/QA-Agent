"""用户记忆的 Qdrant 索引(独立集合、可整库重建;MySQL 才是事实源)。

与知识库索引(app/rag/store.py)的关系:
- **集合独立**:MEMORY_COLLECTION(默认 user_memory)与知识库集合分开,互不干扰;
- **客户端共用**:同一个 Qdrant 实例、同一个连接池(经 rag.store.get_client),
  不再建第二个客户端;但集合与点的读写都在本模块,不经过知识库的「版本集合 / 指针」
  那套发布流程 —— 记忆索引是 MySQL 的派生数据,重建即恢复,不需要 staging / 回滚。

索引形态(**一条记忆 = 一条正文版本 = 一个点**):
- 命名稠密向量 `dense`(Embedding 模型输出,距离 COSINE)+ 命名稀疏向量 `bm25`
  (见 app/memory/text.sparse_terms,服务端 Modifier.IDF)—— 稠密管语义、稀疏管
  关键词,两条通道各自召回后在检索层融合(见 search.py 的 RRF);
- **点 id 带正文版本**:point_id = uuid5(f"{memory_id}:{revision}")。旧版本的点就是
  另一个 id,「旧执行者把新版本覆盖回旧正文」在 Qdrant 层不可能发生(不是靠时间窗口);
  同一正文版本的重复写入是幂等的(同一个点);
- payload 带定位与版本字段(memory_id / user_id / scope / revision / meta_version /
  content_hash / embedding_version / kind / generation / created_at),**不放正文**。
  检索命中之后必须回 MySQL 按这些字段逐项校验(§4.6 的检索侧校验),对不上的点直接丢弃;
  元数据变更(不改正文)只刷新 payload,不重新向量化。

换 Embedding 模型 / 改变维度 / 改词项口径时:embedding_version 会变(见
service.embedding_version),行上的值与之不同 → 全部待重建(见 repo.pending_index_ids);
维度变了要显式调用 recreate_collection() 重建集合 —— 不做自动删集合,免得误删正在
服务的索引。旧集合上的点带着旧的 embedding_version,检索按当前版本过滤,永远召不回来。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Iterable

from qdrant_client.models import (
    Distance, FieldCondition, Filter, FilterSelector, IsEmptyCondition, MatchValue,
    Modifier, PayloadField, PointStruct, Range, ScoredPoint, SparseVector,
    SparseVectorParams, VectorParams,
)

from app.config import get_settings
from app.memory.errors import MemoryConflict, MemoryNotConfigured
from app.memory.models import SCOPE_FORMAL, MemoryItem
from app.memory.text import sparse_terms

logger = logging.getLogger(__name__)

DISTANCE = Distance.COSINE

# 命名向量:稠密(dense)与稀疏(bm25)在同一个集合里,两条通道各自过滤后再融合
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "bm25"

# payload 里放什么(全是定位与版本字段,**不含正文**):
# 检索侧凭这些字段回 MySQL 校验「命中是不是当前事实」,少一个字段就少一道校验
PAYLOAD_FIELDS = (
    "memory_id", "user_id", "scope", "revision", "meta_version", "content_hash",
    "embedding_version", "kind", "generation", "created_at",
)

# point id 的命名空间(固定常量:重建索引时同一个 (memory_id, revision) 必须得到同一个点)
_POINT_NS = uuid.UUID("6f6f2b6a-2a5a-4f39-9d1e-8c2b7d5a1e00")


def get_client():
    """与知识库共用同一个 Qdrant 客户端(连接池只建一份)。

    QDRANT_URL 未配置时把 RAG 那条报错换成记忆口径的 MemoryNotConfigured:
    「记忆索引不可用」与「知识库检索不可用」是两件事,报错要能分清。
    """
    from app.rag.store import get_client as rag_client

    try:
        return rag_client()
    except RuntimeError as exc:
        raise MemoryNotConfigured(
            "未配置 QDRANT_URL:长期记忆用 Qdrant 做可重建索引(事实仍在 MySQL)。"
            "请在 backend/.env 指向你的 Qdrant 后再启用记忆索引。"
        ) from exc


def collection_name() -> str:
    return get_settings().memory_collection


def point_id(memory_id: str, revision: int) -> str:
    """(记忆 id, 正文版本) → Qdrant 点 id(UUID 字符串形态)。

    规则是确定的(重建可复现),且**把版本编进 id**:同一记忆的不同正文版本是不同的点,
    旧执行者的迟到写入落在旧 id 上,既不会覆盖新点、也不是任何查询的候选(见下方说明)。
    """
    return str(uuid.uuid5(_POINT_NS, f"{memory_id}:{int(revision)}"))


def id_of(point: ScoredPoint) -> str:
    """命中的记忆 id(以 payload 为准);payload 缺 memory_id 时返回空串由调用方丢弃。

    不回退到「点 id 反推记忆 id」:带版本的点 id 反推不出记忆 id,猜错了会把别的记忆
    当成候选 —— 宁可丢弃(调用方见 search._dense_candidates)。
    """
    payload = point.payload or {}
    memory_id = payload.get("memory_id")
    return memory_id if isinstance(memory_id, str) else ""


def payload_of(item: MemoryItem, *, embedding_version: str | None = None) -> dict[str, Any]:
    """一条记忆的索引 payload(纯函数:写入与「只刷新 payload」两条路径共用)。

    `embedding_version` 是**这个点所用的口径**;不传就用行上已存的值 —— 那只在
    「行本来就是按当前口径索引好的」时才对(刷新 payload / 对账)。写入新点时**必须显式
    传当前口径**:行上那一列记录的是「上一次索引成功时用的口径」,新行还是空字符串,
    拿它写出去的点在检索侧会被判成「口径不符」而永远召不回来(而且这事不会报错)。
    """
    return {
        "memory_id": item.id,
        "user_id": item.user_id,
        "scope": item.scope,
        "revision": int(item.revision),
        "meta_version": int(item.meta_version),
        "content_hash": item.content_hash,
        "embedding_version": embedding_version or item.embedding_version,
        "kind": item.kind or "",
        "generation": int(item.generation),
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def make_point(item: MemoryItem, vector: list[float], *,
               embedding_version: str | None = None) -> PointStruct:
    """一条记忆 → 一个向量点(稠密 + 稀疏都从这条正文算,payload 不放正文)。

    稀疏向量在这里现算而不是调用方传:写入路径只有这一处,避免出现「有稠密没稀疏」
    的点(那样的点在关键词通道上永远召不回来,且不会报错)。
    `embedding_version` 见 payload_of:写入时必须传当前口径。
    """
    indices, weights = sparse_terms(item.text)
    named: dict[str, Any] = {DENSE_VECTOR: list(vector)}
    if indices:
        named[SPARSE_VECTOR] = SparseVector(indices=indices, values=weights)
    return PointStruct(id=point_id(item.id, int(item.revision)), vector=named,
                       payload=payload_of(item, embedding_version=embedding_version))


# ---- 集合管理 ----

def collection_exists(name: str | None = None) -> bool:
    """集合是否存在(补索引 / 修复路径据此判断「是不是刚建的空集合」)。"""
    try:
        return bool(get_client().collection_exists(name or collection_name()))
    except Exception:  # noqa: BLE001 —— 连不上时按「不知道」处理,由调用方决定是否跳过
        return False


def ensure_collection() -> str:
    """确保记忆集合存在(按配置建空集合:命名稠密 + 命名稀疏);返回集合名。

    并发建集合:两个进程同时建,输的一方收异常,复查存在即接受(与 rag.store 同策略)。
    """
    client = get_client()
    name = collection_name()
    if client.collection_exists(name):
        return name
    try:
        client.create_collection(
            collection_name=name,
            vectors_config={DENSE_VECTOR: VectorParams(
                size=int(get_settings().embeddings_dim), distance=DISTANCE)},
            # 稀疏通道用手写词项 + 服务端 IDF(与降级通道的 BM25 公式同形)
            sparse_vectors_config={SPARSE_VECTOR: SparseVectorParams(modifier=Modifier.IDF)},
        )
        logger.info("已创建记忆向量集合 %s(维度 %d,含稀疏通道)", name,
                    get_settings().embeddings_dim)
    except Exception:  # noqa: BLE001 —— 可能只是并发建集合输了
        if not client.collection_exists(name):
            raise
        logger.info("记忆向量集合 %s 已由其它进程创建,直接沿用", name)
    return name


def collection_state(name: str | None = None) -> dict[str, Any]:
    """集合的形态摘要(状态接口 / 自检用):维度、稀疏通道、与当前配置是否兼容。

    取不到集合时 exists=False。`compatible` 为假时 reason 给出**可执行**的动作
    (改配置或显式重建),不猜、不自动删集合。
    """
    target = name or collection_name()
    try:
        cfg = get_client().get_collection(target).config.params
    except Exception:  # noqa: BLE001 —— 不存在 / 连不上:按不存在报,由调用方决定
        return {"exists": False, "dim": None, "sparse": False, "compatible": False,
                "reason": "集合不存在或不可访问"}
    vectors = cfg.vectors
    dim = None
    if isinstance(vectors, dict):
        params = vectors.get(DENSE_VECTOR)
        dim = int(params.size) if params is not None and params.size else None
    else:  # 旧形态:无名稠密向量(0005 之前建的集合)
        dim = int(getattr(vectors, "size", 0) or 0) or None
    sparse = bool(cfg.sparse_vectors) and SPARSE_VECTOR in (cfg.sparse_vectors or {})
    want = int(get_settings().embeddings_dim)
    reason = ""
    if dim is None:
        reason = "集合里没有 dense 命名向量(旧形态),需要重建集合"
    elif dim != want:
        reason = f"维度为 {dim},当前 Embedding 维度为 {want},需要重建集合"
    elif not sparse:
        reason = "集合缺少 bm25 稀疏向量,需要重建集合"
    return {"exists": True, "dim": dim, "sparse": sparse,
            "compatible": not reason, "reason": reason}


def collection_dim(name: str | None = None) -> int | None:
    """集合的稠密向量维度;集合不存在 / 取不到返回 None(不猜)。"""
    try:
        cfg = get_client().get_collection(name or collection_name()).config.params.vectors
        if isinstance(cfg, dict):
            params = cfg.get(DENSE_VECTOR)
            size = getattr(params, "size", None) if params is not None else None
        else:
            size = getattr(cfg, "size", None)
        return int(size) if size else None
    except Exception:  # noqa: BLE001 —— 集合不存在等一律按「不知道」处理
        return None


def assert_dimension() -> None:
    """写入前检查集合形态与当前配置一致(维度 + 稀疏通道);不一致必须显式重建(不自动删)。

    只比维度是不够的:同一个维度下少建了稀疏通道,关键词通道会一直空着且不报错 ——
    那是「静默降级成只有稠密」,必须在这里拦住。
    """
    state = collection_state()
    if not state["exists"]:
        return                      # 还没建:由 ensure_collection 按当前配置建
    if not state["compatible"]:
        raise MemoryConflict(
            f"记忆集合 {collection_name()} 与当前配置不一致:{state['reason']}。"
            "换模型 / 换维度 / 加稀疏通道后需要显式重建集合(recreate_collection)再索引。"
        )


def recreate_collection() -> str:
    """删掉重建记忆集合(**显式动作**,不自动触发):换 Embedding 维度 / 加稀疏通道时用。

    只删索引不删记忆:重建后由 repo.pending_index_ids + service.reindex 把正文重新
    向量化写回(行上的 embedding_version 因版本不同或标记被清而标脏)。
    """
    client = get_client()
    name = collection_name()
    if client.collection_exists(name):
        client.delete_collection(name)
        logger.warning("已删除记忆向量集合 %s(准备按新配置重建)", name)
    return ensure_collection()


# ---- 写入 ----

def upsert(points: list[PointStruct]) -> int:
    """写入 / 覆盖向量点(集合不存在时按当前配置建)。返回写入条数。

    只有「正文版本一致」的重写才会落在同一个点上(覆盖成同样的内容);正文改版本后
    新旧是两个点,互不覆盖。
    """
    if not points:
        return 0
    client = get_client()
    client.upsert(collection_name=ensure_collection(), points=points)
    return len(points)


def refresh_payload(point_id_value: str, payload: dict[str, Any]) -> bool:
    """只刷新一个点的 payload(元数据变更:正文没变,不重新向量化)。

    返回 False 表示**这个点并不存在**(集合被重建 / 点被别人删掉):调用方必须据此
    走重新向量化的路径,而不是把「payload 刷新了」当成「索引最新」——否则 MySQL 上
    标着已索引、索引里却没有点,检索永远少这一条(见 service._index_items)。
    """
    client = get_client()
    name = collection_name()
    if not client.collection_exists(name):
        return False
    found = client.retrieve(collection_name=name, ids=[point_id_value],
                            with_payload=False, with_vectors=False)
    if not found:
        return False
    client.set_payload(collection_name=name, payload=dict(payload),
                       points=[point_id_value], wait=True)
    return True


def points_exist(ids: Iterable[str]) -> set[str]:
    """批量查询点是否存在(对账用):返回存在的点 id 集合。"""
    wanted = [i for i in dict.fromkeys(ids) if i]
    if not wanted:
        return set()
    client = get_client()
    name = collection_name()
    if not client.collection_exists(name):
        return set()
    records = client.retrieve(collection_name=name, ids=wanted,
                              with_payload=False, with_vectors=False)
    return {str(r.id) for r in records}


def point_payloads(ids: Iterable[str]) -> dict[str, dict]:
    """对账同时核验版本和归属，不能用总点数代替有效点存在性。"""
    wanted = list(dict.fromkeys(ids))
    client = get_client()
    if not wanted or not client.collection_exists(collection_name()):
        return {}
    records = client.retrieve(collection_name=collection_name(), ids=wanted,
                              with_payload=True, with_vectors=False)
    return {str(r.id): dict(r.payload or {}) for r in records}


def points_page(*, scope: str, user_id: str | None = None, offset=None):
    """遍历本作用域的点，供清理崩溃后遗留的孤儿及旧版本。"""
    client = get_client()
    if not client.collection_exists(collection_name()):
        return [], None
    must = [FieldCondition(key="scope", match=MatchValue(value=scope))]
    if user_id is not None:
        must.append(FieldCondition(key="user_id", match=MatchValue(value=user_id)))
    return client.scroll(collection_name=collection_name(), scroll_filter=Filter(must=must),
                         offset=offset, limit=256, with_payload=True, with_vectors=False)


def payload_matches(payload: dict, item: MemoryItem, version: str) -> bool:
    expected = payload_of(item, embedding_version=version)
    return all(payload.get(key) == expected[key] for key in (
        "memory_id", "user_id", "scope", "revision", "meta_version",
        "content_hash", "embedding_version", "generation", "kind"))


# ---- 删除(清理台账执行侧)----

def delete_ids(memory_ids: list[str], *, scope: str | None = None) -> int:
    """按记忆 id 删除它的**全部版本**点(Qdrant 删除幂等:不存在的不报错)。

    按 payload.memory_id 过滤而不是按点 id:点 id 带正文版本,删一条记忆要连它的历史
    版本一起清掉(否则旧版本的点会一直躺在集合里,白白参与(并被丢弃于)检索)。
    返回请求删除的记忆 id 数;集合不存在时返回 0(没有可删的)。
    """
    ids = [i for i in dict.fromkeys(memory_ids) if i]
    if not ids:
        return 0
    client = get_client()
    name = collection_name()
    if not client.collection_exists(name):
        return 0
    conditions: list[Any] = [FieldCondition(key="memory_id", match=MatchValue(value=i))
                             for i in ids]
    must: list[Any] = []
    if scope is not None:
        must.append(FieldCondition(key="scope", match=MatchValue(value=scope)))
    client.delete(collection_name=name, points_selector=FilterSelector(
        filter=Filter(must=must, should=conditions)))
    return len(ids)


def delete_older_revisions(memory_id: str, *, revision: int, scope: str) -> int:
    """清掉这条记忆**早于 revision** 的点(成功发布新版之后调用)。返回清掉的点数。

    带版本的点 id 保证了旧点不会覆盖新点,但旧点也不会自己消失:不清理的话,一条被改了
    很多次的记忆会在索引里留下很多份历史正文,白白挤占候选位。这里删的是
    `revision < N` 的点 —— 正在被别的执行者写的更新版本(N+1)不受影响,本次刚写的 N
    也不受影响,所以并发安全。迟到的旧执行者仍可能重新写回一个旧点(它在自己的事务里
    看到的是旧版本),那由检索侧的逐字段校验兜底丢弃(见 search._validate)。
    """
    client = get_client()
    name = collection_name()
    if not client.collection_exists(name):
        return 0
    flt = Filter(must=[
        FieldCondition(key="memory_id", match=MatchValue(value=memory_id)),
        FieldCondition(key="scope", match=MatchValue(value=scope)),
        FieldCondition(key="revision", range=Range(lt=int(revision))),
    ])
    n = int(client.count(collection_name=name, count_filter=flt).count)
    if n:
        client.delete(collection_name=name, points_selector=FilterSelector(filter=flt))
    return n


def delete_user(user_id: str, *, scope: str, before_generation: int | None = None) -> int:
    """删除某用户在某作用域内的向量点(彻底清除用户记忆数据时用)。返回删除前的点数。

    `before_generation` 是**代次上界**:只删「写入时代次小于它」的点,清除期间用户新写的
    记忆(代次已 +1)不受影响。缺了它就会把清除之后新写入的点一起删掉 —— 那是把用户的
    新记忆静默抹掉。generation 缺失的点(理论上只有历史数据)按最小代次处理,一并清掉。

    **scope 必须显式传**(不设默认值):「宽泛的用户删除」一旦漏掉作用域过滤,正式数据与
    评测数据会互相清掉。
    """
    client = get_client()
    name = collection_name()
    if not client.collection_exists(name):
        return 0
    must: list[Any] = [
        FieldCondition(key="user_id", match=MatchValue(value=user_id)),
        FieldCondition(key="scope", match=MatchValue(value=scope)),
    ]
    if before_generation is None:
        flt = Filter(must=must)
    else:
        flt = Filter(must=must, should=[
            FieldCondition(key="generation", range=Range(lt=int(before_generation))),
            IsEmptyCondition(is_empty=PayloadField(key="generation")),
        ])
    n = int(client.count(collection_name=name, count_filter=flt).count)
    if n:
        client.delete(collection_name=name, points_selector=FilterSelector(filter=flt))
    return n


# ---- 读取 ----

def _scope_filter(user_id: str, scope: str, embedding_version: str | None) -> Filter:
    must: list[Any] = [
        FieldCondition(key="user_id", match=MatchValue(value=user_id)),
        FieldCondition(key="scope", match=MatchValue(value=scope)),
    ]
    if embedding_version:
        # 口径不同的点(旧模型 / 旧分词)不是候选:它们的向量与当前查询不在同一空间
        must.append(FieldCondition(key="embedding_version",
                                   match=MatchValue(value=embedding_version)))
    return Filter(must=must)


def search(query_vector: list[float], *, user_id: str, scope: str, top_k: int,
           embedding_version: str | None = None) -> list[ScoredPoint]:
    """按用户 + 作用域做**稠密**召回;集合不存在返回空。

    分数是余弦相似度(集合按 COSINE 建);阈值 / 融合 / 重排在检索层(search 模块)
    处理,这里只做「取回候选」这一件事。scope 必传(正式=''、评测='eval:<run-id>'):
    少了它评测数据会混进正式检索,反之亦然。
    """
    client = get_client()
    name = collection_name()
    if not client.collection_exists(name):
        return []
    result = client.query_points(
        collection_name=name, query=list(query_vector), using=DENSE_VECTOR,
        query_filter=_scope_filter(user_id, scope, embedding_version),
        limit=max(1, int(top_k)), with_payload=True,
    )
    return list(result.points)


def search_sparse(query: str, *, user_id: str, scope: str, top_k: int,
                  embedding_version: str | None = None) -> list[ScoredPoint] | None:
    """按用户 + 作用域做**稀疏(关键词)**召回。

    返回值的三种含义分得很清(调用方据此决定要不要降级,不能混为一谈):
    - `None`:**这条检索通道用不了** —— 集合不存在、没有稀疏向量、服务端不支持、
      或查询异常。调用方必须退回别的关键词通道并把降级说出去;
    - `[]`:通道可用,但这次没有词项(比如纯符号查询)或没有命中 —— 是正常结果;
    - 非空:命中的点(仍只是候选,要过 MySQL 校验)。

    编码用 text.sparse_terms —— 与写入侧同一个函数,保证查询与索引在同一空间。
    """
    indices, weights = sparse_terms(query)
    if not indices:
        return []
    client = get_client()
    name = collection_name()
    if not client.collection_exists(name):
        return None
    try:
        result = client.query_points(
            collection_name=name,
            query=SparseVector(indices=indices, values=weights), using=SPARSE_VECTOR,
            query_filter=_scope_filter(user_id, scope, embedding_version),
            limit=max(1, int(top_k)), with_payload=True,
        )
    except Exception as exc:  # noqa: BLE001 —— 集合没有稀疏通道 / 服务端不支持:显式降级
        logger.warning("记忆稀疏检索不可用(降级到内存 BM25):%s", exc)
        return None
    return list(result.points)


def count(user_id: str | None = None, *, scope: str | None = None) -> int:
    """集合里的点数(可按用户 / 作用域过滤);集合不存在返回 0。

    注意:一条记忆可能对应多个点(正文改过版本),所以这个数与「记忆条数」不是一回事,
    只用于诊断 / 对账(见 worker 的漂移检查)。
    """
    client = get_client()
    name = collection_name()
    if not client.collection_exists(name):
        return 0
    must: list[Any] = []
    if user_id is not None:
        must.append(FieldCondition(key="user_id", match=MatchValue(value=user_id)))
    if scope is not None:
        must.append(FieldCondition(key="scope", match=MatchValue(value=scope)))
    flt = Filter(must=must) if must else None
    return int(client.count(collection_name=name, count_filter=flt).count)
