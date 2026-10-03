"""embedding 结果缓存(第 3 层):qa:cache:embedding:v1:<digest>,避免重复提交相同切块。

只服务**索引侧**的批量向量化。用户查询(检索)不走这里 —— 查询向量直接调 embedding
客户端,检索不依赖缓存是否可用(§4.3 修订;见 retrieve.search_knowledge)。

键 = 实际发送给模型的完整文本 + 供应商端点 + 模型名 + 维度 + 人工版本标识(§4.3)。
值 = {"vector": [...]},读取时校验数值有效性与维度;维度不符 / 非数字 → 按未命中重算并覆盖。
换模型或换维度 → 键自然不同,整批重新向量化(§5 表)。

来源路径不进键也不进值:相同文本的向量在不同文件间复用,各自的 Qdrant 点仍分别写入
来源字段,不因复用向量而合并文件(§4.3)。

计算锁(§5):未命中文本各自抢锁,先抢到的一起批量提交;抢不到的限时等待并复查缓存,
等不到抛 CacheBusy,**绝不在没有锁时自行计算**。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import datetime, timezone

from app.config import get_settings
from app.rag.cache_store import (
    CACHE_PREFIX, CacheBackend, CacheBusy, LockRenewer, get_cache, load_json, lock_name,
    write_with_retry,
)

logger = logging.getLogger(__name__)

KEY_VERSION = "v1"
_PROVIDER = "openai-compatible"   # 端点形态:OpenAI 兼容接口(base_url 区分具体供应商)


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _config() -> dict:
    s = get_settings()
    return {
        "provider": _PROVIDER,
        "endpoint": (s.embeddings_base_url or "").rstrip("/"),
        "model": s.embeddings_model,
        "dim": int(s.embeddings_dim),
        "version": s.embeddings_version,
    }


def embedding_digest(text: str) -> str:
    return _digest({**_config(), "text": text})


def cache_key(text: str) -> str:
    return f"{CACHE_PREFIX}embedding:{KEY_VERSION}:{embedding_digest(text)}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _vector_of(value: str | None, dim: int) -> list[float] | None:
    """缓存值 → 向量;形态 / 维度 / 数值不对返回 None(= 未命中,重算后覆盖)。"""
    payload = load_json(value, what="embedding 缓存")
    if not isinstance(payload, dict):
        return None
    vec = payload.get("vector")
    if not isinstance(vec, list) or not vec:
        return None
    if len(vec) != dim:
        logger.warning("embedding 缓存维度 %d 与配置 %d 不符,按未命中重算", len(vec), dim)
        return None
    try:
        out = [float(x) for x in vec]
    except (TypeError, ValueError):
        logger.warning("embedding 缓存含非数字,按未命中重算")
        return None
    if not all(math.isfinite(x) for x in out):
        logger.warning("embedding 缓存含非有限数值,按未命中重算")
        return None
    return out


def _valid_vector(vec, dim: int) -> bool:
    """新算出的向量在写缓存前先校验:维度不对 / 非有限数值一律不写(§11 验收)。"""
    if not isinstance(vec, list) or len(vec) != dim:
        return False
    try:
        return all(math.isfinite(float(x)) for x in vec)
    except (TypeError, ValueError):
        return False


def _encode(vec: list[float]) -> str:
    return json.dumps({"schema": 1, "kind": "embedding", **_config(), "dim": len(vec),
                       "vector": vec, "created_at": _now()}, ensure_ascii=False)


def _store_many(cache: CacheBackend, keys: list[str], vectors: list[list[float]], dim: int) -> None:
    """逐个整值写入(不写多步半成品);非法向量不写缓存,并立刻抛错中止该文件。

    维度错误 / 非数字向量既不进缓存(§11),也不该被当成"这次算成功了"继续往下走。
    """
    bad = 0
    for key, vec in zip(keys, vectors):
        if not _valid_vector(vec, dim):
            bad += 1
            logger.warning("embedding 返回的向量维度 / 数值不合法,不写缓存:%s", key.rsplit(":", 1)[-1][:12])
            continue
        write_with_retry(cache, key, _encode([float(x) for x in vec]))
    if bad:
        raise RuntimeError(f"embedding 返回了 {bad} 条非法向量(维度应为 {dim}),已中止且未写入缓存")


def embed_documents(client, texts: list[str]) -> list[list[float]]:
    """批量向量化(带缓存):命中直接复用,未命中的**持锁后**一次批量提交。

    同一批里的重复文本先去重,只提交一次、共用同一向量(§4.3)。
    没抢到锁的文本:最多等 cache_lock_wait_seconds,期间反复重取锁并复查缓存;
    等不到就抛 CacheBusy —— 可重试的忙碌状态,**绝不在没有锁时自行计算**(§5)。
    批量调用可能超出锁 TTL,持锁期间由后台线程按 TTL 的一半续期。
    """
    if not texts:
        return []
    cache = get_cache()
    dim = _config()["dim"]
    uniq = list(dict.fromkeys(texts))                  # 顺序去重:同一批里的重复文本只算一次
    keys = {t: cache_key(t) for t in uniq}

    got: dict[str, list[float] | None] = {
        t: _vector_of(raw, dim) for t, raw in zip(uniq, cache.mget([keys[t] for t in uniq]))}
    missing = [t for t in uniq if got[t] is None]
    if not missing:
        return [got[t] for t in texts]  # type: ignore[misc]

    ttl = float(get_settings().cache_lock_ttl_seconds)
    wait = float(get_settings().cache_lock_wait_seconds)
    tokens: dict[str, str] = {}
    renewer: LockRenewer | None = None
    try:
        deadline = time.monotonic() + wait
        while True:
            for t in [t for t in missing if t not in tokens]:
                token = cache.acquire_lock(lock_name("embedding", embedding_digest(t)), ttl)
                if token:
                    tokens[t] = token
            pending = [t for t in missing if t not in tokens and got[t] is None]
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(min(0.25, max(0.05, wait / 10)))
            for t, raw in zip(pending, cache.mget([keys[t] for t in pending])):
                got[t] = _vector_of(raw, dim)              # 等锁期间别人可能已写好
        if pending:
            raise CacheBusy(f"embedding:{len(pending)} 条文本")

        mine = [t for t in tokens if got[t] is None]
        if mine:                                           # 持锁后复查:抢锁前别人可能已写好
            for t, raw in zip(mine, cache.mget([keys[t] for t in mine])):
                got[t] = _vector_of(raw, dim)
            mine = [t for t in mine if got[t] is None]

        if mine:
            renewer = LockRenewer(cache, [(lock_name("embedding", embedding_digest(t)), tokens[t])
                                          for t in mine], ttl)   # 只续真正在算的这几把
            renewer.start()
            vectors = client.embed_documents(mine)
            if len(vectors) != len(mine):
                raise RuntimeError(f"embedding 返回 {len(vectors)} 条,与请求 {len(mine)} 条不符")
            _store_many(cache, [keys[t] for t in mine], vectors, dim)
            for t, vec in zip(mine, vectors):
                got[t] = [float(x) for x in vec]
    finally:
        if renewer is not None:
            renewer.stop()
        for t, token in tokens.items():
            try:
                cache.release_lock(lock_name("embedding", embedding_digest(t)), token)
            except Exception as exc:   # 释放失败只能等 TTL
                logger.warning("释放 embedding 计算锁失败(等 TTL 过期):%s", exc)

    return [got[t] for t in texts]  # type: ignore[misc]
