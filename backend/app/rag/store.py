"""RAG 向量库:Qdrant 薄封装 + 索引「版本集合 / 发布 / 回退」。

只连真实 Qdrant,连接靠 .env 的 QDRANT_URL / QDRANT_API_KEY;未配置 QDRANT_URL
直接抛错,不留内存兜底(保持生产代码干净)。向量化在 rag/embeddings.py,这里只吃向量。

版本发布(替代原先的「先删旧再重建」):
  物理集合命名 `{QDRANT_COLLECTION}__<UTC时间戳>`;读取永远走 `active_collection()` ——
  它读 `{INDEX_STATE_DIR}/index-manifest.json` 里的指针,没有 manifest 时退回配置里的物理名
  (即迁移前状态,现有线上集合原样可用,零改动)。

  发布流程:建候选集合(create_staging)→ 复制范围外旧向量(copy_points)→ 写本次范围 →
  校验数量/维度(publish)→ 原子翻转指针;旧集合登记为 previous 供回退,两代之前的版本集合
  在下次发布时清理。整库任务不复制(全量重建)。

  注意:这里用「物理集合名 + 指针」而非 Qdrant 别名,原因见 docs/OCR接入技术方案.md §9 ——
  别名与集合共用同一命名空间,现有配置指向的物理名已占用,建同名别名会直接冲突;指针方案
  对现网零风险,且能显式记录 来源清单 / 阶段 / 回退版本。代价是读取方必须一律经
  active_collection(),不能硬编码配置名 —— 本模块已全部收口。

  Qdrant 指针切换不是跨系统事务:OSS、任务 JSON、Qdrant 三者只做到「可对账、可回退」,
  不声称整体原子化。发布期间文件变更会使本次发布被拒绝(见 ingest.publish_job)。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    ScoredPoint,
    VectorParams,
)

from app.config import get_settings
from app.rag.localfs import atomic_write_json, read_json

logger = logging.getLogger(__name__)

MANIFEST_NAME = "index-manifest.json"
_HISTORY_CAP = 50
_COPY_BATCH = 128


@lru_cache
def get_client() -> QdrantClient:
    s = get_settings()
    if not s.qdrant_url:
        raise RuntimeError(
            "未配置 QDRANT_URL:请在 backend/.env 指向你的 Qdrant"
            "(如 http://<ECS-IP>:6333)后再使用 RAG。"
        )
    return QdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key or None)


# ---- 版本指针(manifest) ----

def manifest_path() -> Path:
    """manifest 与 OCR 缓存同目录(backend/data/ocr),部署时随缓存一起持久化。"""
    return Path(get_settings().index_state_dir) / MANIFEST_NAME


def read_manifest() -> dict:
    """读版本指针;不存在 / 损坏时返回 {}(= 尚未发布过版本,读写走配置里的物理名)。"""
    data = read_json(manifest_path(), default={})
    return data if isinstance(data, dict) else {}


def _write_manifest(data: dict) -> None:
    """原子写:发布切换不会读到半截 JSON(staging/active 指针要么旧要么新)。"""
    atomic_write_json(manifest_path(), data)


def base_name() -> str:
    """逻辑集合名(=.env 的 QDRANT_COLLECTION);版本集合 = 它 + '__时间戳'。"""
    return get_settings().qdrant_collection


def active_collection() -> str:
    """当前生效的物理集合名。所有读写都必须经这里取名字。"""
    m = read_manifest()
    name = m.get("active")
    return name if isinstance(name, str) and name else base_name()


def versions() -> list[dict]:
    """本知识库现有的版本集合(含 active / previous / staging 标记),供运维查看。"""
    client = get_client()
    base = base_name()
    m = read_manifest()
    out: list[dict] = []
    for c in client.get_collections().collections:
        n = c.name
        if n != base and not n.startswith(f"{base}__"):
            continue
        role = "legacy" if n == base else "version"
        if n == m.get("active"):
            role = "active"
        elif n == m.get("previous"):
            role = "previous"
        elif n == m.get("staging"):
            role = "staging"
        out.append({"name": n, "role": role, "points": collection_count(n)})
    out.sort(key=lambda x: x["name"])
    return out


# ---- 集合 ----

def collection_exists(name: str) -> bool:
    return get_client().collection_exists(name)


def collection_count(name: str) -> int:
    return get_client().count(collection_name=name, exact=True).count


def collection_dim(name: str) -> int | None:
    """集合的向量维度;取不到返回 None(单向量配置之外的形态不猜)。"""
    try:
        cfg = get_client().get_collection(name).config.params.vectors
        size = getattr(cfg, "size", None)
        return int(size) if size else None
    except Exception:
        return None


def _create(name: str) -> str:
    s = get_settings()
    get_client().create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=s.embeddings_dim, distance=Distance.COSINE),
    )
    return name


def ensure_collection() -> str:
    """当前生效集合,不存在就按空集合建(全新部署 / 首次索引)。返回物理名。"""
    name = active_collection()
    if not collection_exists(name):
        _create(name)
    return name


def _version_name() -> str:
    """版本集合名 = `{基础名}__<UTC时间戳>-<4位随机>`。

    时间戳便于人读,随机后缀避免同一秒内两次建候选撞名(测试与连续小范围重建都会遇到)。
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{base_name()}__{ts}-{uuid.uuid4().hex[:4]}"


def create_staging() -> str:
    """为一次发布任务建候选集合,并把 staging 写进 manifest(重启后可对账清理)。"""
    name = _version_name()
    _create(name)
    m = read_manifest()
    if m.get("staging") and m["staging"] != name:
        logger.warning("上一次的候选集合 %s 仍在 manifest 里,本次直接覆盖记录", m["staging"])
    m.update({"collection": base_name(), "staging": name, "updated_at": _now()})
    _write_manifest(m)
    logger.info("创建候选集合 %s(当前生效:%s)", name, active_collection())
    return name


def abort_staging(name: str) -> None:
    """放弃候选集合:删除物理集合并清掉 staging 记录(尽力而为,失败只告警)。"""
    client = get_client()
    try:
        if name != base_name() and name != active_collection() and collection_exists(name):
            client.delete_collection(name)
    except Exception as exc:
        logger.warning("候选集合 %s 删除失败(留待人工清理):%s", name, exc)
    m = read_manifest()
    if m.get("staging") == name:
        m["staging"] = None
        m["updated_at"] = _now()
        _write_manifest(m)


def copy_points(src: str, dst: str, keep) -> tuple[int, int]:
    """把 src 中 keep(payload)==True 的点(含向量与 payload)复制进 dst。

    用于子树 / 单文件任务保住范围外内容。返回 (复制条数, 丢弃条数);返回 id 原样保留,
    同一份原件在发布前后的 point id 恒定,便于对账。
    """
    client = get_client()
    copied = dropped = 0
    buf: list[PointStruct] = []
    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=src, with_payload=True, with_vectors=True,
            limit=256, offset=offset,
        )
        for r in records:
            if not keep(r.payload or {}):
                dropped += 1
                continue
            buf.append(PointStruct(id=r.id, vector=r.vector, payload=r.payload or {}))
        if len(buf) >= _COPY_BATCH:
            client.upsert(collection_name=dst, points=buf)
            copied += len(buf)
            buf = []
        if offset is None:
            break
    if buf:
        client.upsert(collection_name=dst, points=buf)
        copied += len(buf)
    return copied, dropped


def publish(name: str, *, expected_count: int | None = None, note: str = "") -> dict:
    """校验候选集合并把指针切过去:active←name,previous←旧 active。返回新 manifest。

    校验不通过直接抛 RuntimeError,指针保持原样 —— 旧版本继续可检索,这是失败保护的核心。
    """
    if name == base_name():
        raise RuntimeError("不接受把指针指向基础集合名")
    if not collection_exists(name):
        raise RuntimeError(f"候选集合 {name} 不存在,拒绝发布")
    if name == active_collection():
        raise RuntimeError("候选集合与当前生效集合相同,拒绝发布")

    dim = collection_dim(name)
    want = get_settings().embeddings_dim
    if dim is not None and dim != want:
        raise RuntimeError(f"候选集合维度 {dim} 与配置 {want} 不符,拒绝发布")
    got = collection_count(name)
    if expected_count is not None and got != expected_count:
        raise RuntimeError(f"候选集合点数 {got} 与本次写入预期 {expected_count} 不符,拒绝发布")

    m = read_manifest()
    old = active_collection()
    m.update({
        "collection": base_name(),
        "active": name,
        "previous": old,
        "staging": None,
        "updated_at": _now(),
    })
    hist = list(m.get("history") or [])
    hist.append({"at": _now(), "action": "publish", "name": name, "replaced": old,
                 "points": got, "note": note})
    m["history"] = hist[-_HISTORY_CAP:]
    _write_manifest(m)
    logger.info("索引已发布:%s(← %s),点数 %d", name, old, got)
    _cleanup_old(m)
    return m


def rollback() -> dict:
    """把指针切回上一个版本(previous)。没有可回退版本时抛 RuntimeError。"""
    m = read_manifest()
    cur, prev = m.get("active"), m.get("previous")
    if not prev:
        raise RuntimeError("没有可回退的上一版本(manifest 里没有 previous)")
    if not collection_exists(prev):
        raise RuntimeError(f"上一版本集合 {prev} 已不存在,无法回退")
    m.update({"active": prev, "previous": cur, "updated_at": _now()})
    hist = list(m.get("history") or [])
    hist.append({"at": _now(), "action": "rollback", "name": prev, "replaced": cur,
                 "points": collection_count(prev), "note": ""})
    m["history"] = hist[-_HISTORY_CAP:]
    _write_manifest(m)
    logger.warning("索引已回退:%s(← %s)", prev, cur)
    return m


def _cleanup_old(m: dict) -> None:
    """删除两代之前的版本集合(不碰基础集合名,也不碰 active / previous / staging)。"""
    keep = {base_name(), m.get("active"), m.get("previous"), m.get("staging")}
    try:
        names = [c.name for c in get_client().get_collections().collections]
    except Exception as exc:
        logger.warning("列举集合失败,跳过清理:%s", exc)
        return
    for n in names:
        if n in keep or not n.startswith(f"{base_name()}__"):
            continue
        try:
            get_client().delete_collection(n)
            logger.info("清理过期版本集合 %s", n)
        except Exception as exc:
            logger.warning("过期版本集合 %s 清理失败:%s", n, exc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- 点位读写(一律作用于 active_collection) ----

def make_point(point_id: str | int, vector: list[float], payload: dict) -> PointStruct:
    """构造一个向量点(把 Qdrant 的 PointStruct 细节收在本模块内,摄取层不直接依赖 qdrant)。"""
    return PointStruct(id=point_id, vector=vector, payload=payload)


def upsert(points: list[PointStruct], *, collection: str | None = None) -> int:
    """写入 / 覆盖向量点,返回写入条数。默认写当前生效集合,发布期间可指定候选集合。"""
    if not points:
        return 0
    name = collection or ensure_collection()
    get_client().upsert(collection_name=name, points=points)
    return len(points)


def delete_by_oss_key(key: str, *, collection: str | None = None) -> int:
    """删除某原件的全部向量点(按 payload oss_key 精确匹配)。返回删除前匹配到的点数。

    用于增量维护:删原件时连带清索引。collection 不存在时视作 0(best-effort:Qdrant
    没接通也不该让"删原件"报错)。删除只作用于当前生效集合 —— 发布期间删除的目标文件
    不会在新版本里复活(candidate 只抄了发布时刻的旧点,见 ingest)。
    """
    client = get_client()
    name = collection or active_collection()
    if not client.collection_exists(name):
        return 0
    flt = Filter(must=[FieldCondition(key="oss_key", match=MatchValue(value=key))])
    n = client.count(collection_name=name, count_filter=flt).count
    if n:
        client.delete(collection_name=name, points_selector=FilterSelector(filter=flt))
    return n


def keys_in(collection: str | None = None) -> set[str]:
    """整个生效集合里已索引的原件 key 集合(去重)。"""
    client = get_client()
    name = collection or active_collection()
    if not client.collection_exists(name):
        return set()
    keys: set[str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=name, with_payload=["oss_key"], with_vectors=False,
            limit=512, offset=offset,
        )
        for p in points:
            k = (p.payload or {}).get("oss_key")
            if k:
                keys.add(k)
        if offset is None:
            break
    return keys


def indexed_keys(prefix: str = "") -> set[str]:
    """返回某分类节点(payload category == prefix)下已建立索引的原件 key 集合。

    prefix 为知识库相对前缀(根为空串,其余以 / 结尾),与摄取写入的 category 对齐,
    故只覆盖该节点直属文件(不含子节点)—— 正好供前端逐层浏览时显示已/未索引徽标。
    collection 不存在时返回空集。
    """
    client = get_client()
    name = active_collection()
    if not client.collection_exists(name):
        return set()
    flt = Filter(must=[FieldCondition(key="category", match=MatchValue(value=prefix))])
    keys: set[str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=name,
            scroll_filter=flt,
            with_payload=["oss_key"],
            with_vectors=False,
            limit=256,
            offset=offset,
        )
        for p in points:
            k = (p.payload or {}).get("oss_key")
            if k:
                keys.add(k)
        if offset is None:
            break
    return keys


def search(query_vector: list[float], top_k: int = 5) -> list[ScoredPoint]:
    """按向量检索当前生效集合,返回命中(含 payload 与 score),按相关度降序。"""
    client = get_client()
    name = ensure_collection()
    result = client.query_points(
        collection_name=name,
        query=query_vector,
        limit=max(1, top_k),
        with_payload=True,
    )
    return result.points
