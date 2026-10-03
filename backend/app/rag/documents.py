"""文件身份与缓存清单:document_id 登记、路径映射、正式 / 待发布清单、发布后清理。

Redis 键(方案 §6 / §7):
  qa:path:<路径标识摘要>              → document_id(摘要含桶 + 配置根前缀 + 规范化相对 key)
  qa:document:<document_id>:record   → 当前相对 key、内容哈希、状态
  qa:document:<document_id>:manifest → 当前**已发布版本**用了哪些缓存键(无 TTL)

身份规则(§6):首次上传 / 登记生成 UUID;重新索引、经应用改名移动、内容更新保持 ID;
删除后重新上传是新的 ID;同一路径并发登记靠 SET NX 原子完成,只有一个有效 ID。

清理规则(§8):发布成功后删除 `旧清单键 − 新清单键`;删除文件先落清单快照,原件与索引
删完再清缓存与登记;失败保留恢复记录(本地 JSON)供启动对账重试,只重试删除,不重跑 OCR。
删除只按清单里明确列出的键执行,不做 FLUSHDB、不按前缀模糊删除。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.rag.cache_store import (
    DOCUMENT_PREFIX, PATH_PREFIX, CacheBackend, CacheError, get_cache, load_json,
)
from app.rag.localfs import atomic_write_json, read_json

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_REGISTER_RETRIES = 5     # 并发登记落败后读回赢家 ID 的尝试次数


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_key(key: str) -> str:
    """规范化相对 key:只去首尾空白与前导 '/',不改大小写(OSS key 区分大小写,§6)。"""
    return (key or "").strip().lstrip("/")


def _namespace() -> str:
    s = get_settings()
    root = (s.oss_prefix or "").strip().lstrip("/")
    if root and not root.endswith("/"):
        root += "/"
    return f"oss://{s.oss_bucket}/{root}"


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def path_digest(key: str) -> str:
    """路径标识摘要:含桶 + 配置根前缀 + 规范化相对 key,不同环境 / 桶互不混用(§6)。"""
    return _digest({"namespace": _namespace(), "key": _norm_key(key)})


def path_key(key: str) -> str:
    return f"{PATH_PREFIX}{path_digest(key)}"


def record_key(document_id: str) -> str:
    return f"{DOCUMENT_PREFIX}{document_id}:record"


def manifest_key(document_id: str) -> str:
    return f"{DOCUMENT_PREFIX}{document_id}:manifest"


# ---- 记录目录(发布阶段 / 删除恢复记录;本地 JSON,任务状态之外的恢复凭据)----

def _records_dir(kind: str) -> Path:
    return Path(get_settings().index_state_dir) / kind


def publish_record_path(job_id: str) -> Path:
    return _records_dir("manifests") / f"{job_id}.json"


def deletion_record_path(document_id: str) -> Path:
    return _records_dir("deletions") / f"{document_id}.json"


# ---- 登记(§6)----

def _being_deleted(cache: CacheBackend, doc_id: str) -> bool:
    """该 document_id 是否正处于删除流程中(登记带 deleting 标记且本地恢复记录还在)。

    先查本地文件再查 Redis:没有删除记录的常规路径只多一次本地 stat,不增加 Redis 往返。
    恢复记录已被撤销/执行完时,残留的 deleting 标记不再生效(下次登记自动纠正)。
    """
    if not deletion_record_path(doc_id).exists():
        return False
    stored = load_json(cache.get(record_key(doc_id)), what="文件登记")
    return isinstance(stored, dict) and stored.get("status") == "deleting"


def _claim_id(cache: CacheBackend, key: str) -> str:
    """取该路径的 document_id:已登记就复用,否则原子登记一个新 UUID。

    SET NX 保证同一路径并发登记只有一个赢家;落败方读回赢家 ID(赢家还没写 record 时
    由落败方的 set_if_absent 补上,两边内容一致)。

    正被删除的旧身份(§8):该路径视为空,重新登记新 ID —— 否则同路径重传会继承旧
    document_id,随后被延迟执行的删除恢复误伤(删掉新文件仍在用的清单与登记)。
    """
    pkey = path_key(key)
    got = cache.get(pkey)
    if got and not _being_deleted(cache, got):
        return got
    if got:
        cache.delete([pkey])            # 待删除:只在「该值已作废」时清,再由 SET NX 决定赢家
    for _ in range(_REGISTER_RETRIES):
        candidate = str(uuid.uuid4())
        if cache.set_if_absent(pkey, candidate):
            return candidate
        got = cache.get(pkey)
        if got:
            return got
        time.sleep(0.05)
    raise CacheError(f"路径登记并发冲突,未能读到已登记的 document_id:{key}")


def register(key: str, *, content_sha256: str = "") -> str:
    """登记 / 复用文件身份,返回 document_id(缓存关闭时返回 "")。

    已有文件首次访问或索引时登记;登记不依赖首次索引成功(§6)。内容哈希变了只更新
    记录(保持同一个 ID)—— 那是「经应用更新文件内容」,不是新文件。
    """
    cache = get_cache()
    if not cache.enabled:
        return ""
    k = _norm_key(key)
    if not k:
        return ""
    doc_id = _claim_id(cache, k)
    rkey = record_key(doc_id)
    stored = load_json(cache.get(rkey), what="文件登记")
    if not isinstance(stored, dict) or not stored:
        created = {
            "schema_version": SCHEMA_VERSION, "document_id": doc_id, "namespace": _namespace(),
            "oss_key": k, "content_sha256": content_sha256, "status": "active",
            "created_at": _now(), "updated_at": _now(),
        }
        if not cache.set_if_absent(rkey, json.dumps(created, ensure_ascii=False)):
            stored = load_json(cache.get(rkey), what="文件登记")
        else:
            return doc_id
    if isinstance(stored, dict):
        changed = (stored.get("oss_key") != k
                   or (content_sha256 and stored.get("content_sha256") != content_sha256))
        if changed:
            stored.update({"oss_key": k, "namespace": _namespace(),
                           "content_sha256": content_sha256 or stored.get("content_sha256", ""),
                           "updated_at": _now()})
            cache.set(rkey, json.dumps(stored, ensure_ascii=False))
    return doc_id


def get_id(key: str) -> str | None:
    """路径 → document_id;未登记返回 None。"""
    cache = get_cache()
    if not cache.enabled:
        return None
    return cache.get(path_key(key))


def move(key: str, new_key: str) -> str | None:
    """经应用改名 / 移动:保持 document_id 与全部缓存,只更新路径映射(§6)。

    应用当前没有改名 / 移动入口,这里先提供能力(供以后接入),不挂 API / UI;
    直接在 OSS 控制台移动对象不会自动延续身份。
    """
    cache = get_cache()
    old, new = _norm_key(key), _norm_key(new_key)
    doc_id = get_id(old) if old else None
    if not doc_id or not new:
        return None
    cache.set(path_key(new), doc_id)
    if old != new:
        cache.delete([path_key(old)])
    stored = load_json(cache.get(record_key(doc_id)), what="文件登记")
    if isinstance(stored, dict):
        stored.update({"oss_key": new, "updated_at": _now()})
        cache.set(record_key(doc_id), json.dumps(stored, ensure_ascii=False))
    return doc_id


# ---- 文件缓存清单(§7)----

def build_manifest(*, document_id: str, oss_key: str, content_sha256: str, extraction_mode: str,
                   mixed_invoice: bool, index_version: str,
                   pages: list[tuple[int, str | None, str | None]],
                   embedding_keys: list[str]) -> dict:
    """构造一份文件的正式清单(只记「当前已发布版本」用了哪些缓存,不记引用次数)。

    pages: (页码, 识别缓存键, 转换缓存键) —— 空白页 / 原生页可以两个键都为空(不记录该页);
    embedding_keys: 该文件切块的向量缓存键(去重、保持首次出现顺序)。
    """
    page_rows = [
        {"page_number": p, "ocr_cache_key": o, "text_cache_key": t}
        for (p, o, t) in pages if o or t
    ]
    seen: set[str] = set()
    emb: list[str] = []
    for k in embedding_keys:
        if k and k not in seen:
            seen.add(k)
            emb.append(k)
    return {
        "schema_version": SCHEMA_VERSION,
        "document_id": document_id,
        "oss_key": _norm_key(oss_key),
        "content_sha256": content_sha256,
        "extraction_mode": extraction_mode,
        "mixed_invoice": bool(mixed_invoice),
        "index_version": index_version,
        "pages": page_rows,
        "embedding_cache_keys": emb,
        "updated_at": _now(),
    }


def cache_keys_of(manifest: dict | None) -> set[str]:
    """清单里列出的全部缓存键(旧清单同样适用;缺失 / 形态不对按空集)。"""
    if not isinstance(manifest, dict):
        return set()
    keys = {k for k in (manifest.get("embedding_cache_keys") or []) if k}
    for row in manifest.get("pages") or []:
        if not isinstance(row, dict):
            continue
        for name in ("ocr_cache_key", "text_cache_key"):
            if row.get(name):
                keys.add(row[name])
    return keys


def write_manifest(manifest: dict) -> None:
    get_cache().set(manifest_key(manifest["document_id"]), json.dumps(manifest, ensure_ascii=False))


def manifest_of(document_id: str) -> dict | None:
    payload = load_json(get_cache().get(manifest_key(document_id)), what="文件缓存清单")
    return payload if isinstance(payload, dict) else None


# ---- 发布阶段记录与发布后清理(§8)----

def prepare_publish(job_id: str, index_version: str, manifests: dict[str, dict]) -> Path | None:
    """发布前把待发布清单持久化进任务记录(§8 步骤 1);没有清单返回 None。

    只写本地 JSON:此时还没发布,记录状态 prepared —— 发布失败就永远停在 prepared,
    不会被恢复流程执行(恢复只处理 publishing / prepared 且版本已发布的记录)。

    同时把每份文件的**旧清单快照与待清理键**一并落盘:快照必须取自发布前这一刻,
    恢复时不能重读当前清单来"补算"(那时可能已经被更新的发布改过了)。
    """
    if not manifests:
        return None
    cache = get_cache()
    files = {}
    for doc_id, m in manifests.items():
        old = manifest_of(doc_id)
        files[doc_id] = {
            "oss_key": m.get("oss_key", ""), "new": m, "old": old,
            "stale_keys": sorted(cache_keys_of(old) - cache_keys_of(m)),
        }
    path = publish_record_path(job_id)
    atomic_write_json(path, {
        "schema_version": SCHEMA_VERSION, "job_id": job_id, "index_version": index_version,
        "status": "prepared", "created_at": _now(), "updated_at": _now(),
        "files": files,
    })
    return path


def _plan_entry(rec: dict, entry: dict, cur: dict | None) -> tuple[bool, list[str], str]:
    """决定一份文件这次恢复要不要写清单、要删哪些键(§8)。

    依据是「文件当前清单」与「本次任务记录的快照」的关系 —— 不看全库当前版本:
    - cur 就是本次任务写的(版本相同)→ 续跑,重写幂等;
    - cur 还是发布前的旧清单 → 补写(崩在写清单之前);
    - cur 是**更新的发布**写的 → 过期任务:不覆盖清单,只补删旧键里当前版本不在用的;
    - 清单整个不见了(文件被删除 / 清理中)→ 不补写、不删键(不猜范围)。

    返回 (是否写清单, 待删键, 跳过原因)。
    """
    new = entry.get("new") or {}
    old = entry.get("old")
    stale = list(entry.get("stale_keys") or [])
    version = rec.get("index_version")
    if cur is None:
        if old is None:
            return True, [], ""                  # 首次发布:旧清单本来就不存在
        return False, [], "清单已不存在(文件可能已被删除),不补写也不删键"
    cur_version = cur.get("index_version")
    if cur_version == version or (isinstance(old, dict) and cur_version == old.get("index_version")):
        return True, stale, ""               # stale 本身就是「旧清单 − 新清单」,不需要再减
    in_use = cache_keys_of(cur)              # 更新的发布在用了:只删它没用到的部分
    return False, [k for k in stale if k not in in_use], "已被更新的发布接管,只补删当前版本不在用的旧键"


def apply_publish(job_id: str, *, active_collection: str | None = None) -> dict:
    """发布成功后:写正式清单 → 删除「旧 − 新」缓存键(§8 步骤 2–4)。

    两个阶段分开推进、都可重入:先按记录里的快照逐文件核对当前清单并写正式清单,
    落盘标记后再删键。任一步失败都保留记录,重试只补做没完成的阶段,不重新 OCR(§8)。

    过期任务的恢复不会再覆盖较新的清单,也不会删除当前版本仍在用的缓存键。
    返回 {"applied", "files", "deleted_keys", "pending_keys", "skipped"}。
    """
    path = publish_record_path(job_id)
    rec = read_json(path, default=None)
    if not isinstance(rec, dict):
        raise CacheError(f"找不到待发布清单记录:{path}")
    if rec.get("status") == "applied":
        return {"status": "applied", "files": len(rec.get("files") or {}), "deleted_keys": 0,
                "retained_keys": 0, "skipped": "already_applied"}
    if rec.get("status") == "prepared":
        # 崩在发布与收尾之间:只有确认本次版本确实是当前生效版本才继续(没有别的信号能
        # 证明这次真的发布过)。publishing 记录不走这道门 —— 它已经发布成功,按文件核对。
        if not active_collection or rec.get("index_version") != active_collection:
            return {"status": "prepared", "files": 0, "deleted_keys": 0, "retained_keys": 0,
                    "skipped": "未确认发布,不更新清单、不清理缓存"}
    cache = get_cache()
    files: dict = rec.setdefault("files", {})

    plan: list[tuple[str, dict, bool, list[str]]] = []    # (doc_id, entry, 写不写, 待删键)
    for doc_id, entry in files.items():
        if entry.get("applied"):
            continue
        cur = manifest_of(doc_id)
        write, delete_keys, why = _plan_entry(rec, entry, cur)
        entry["skipped"] = why
        plan.append((doc_id, entry, write, delete_keys))

    # 阶段一:更新正式清单(先落盘计划,再写清单;再崩也只重做这一阶段,可重入)
    rec["status"] = "publishing"
    rec["phase"] = "manifests"
    rec["updated_at"] = _now()
    atomic_write_json(path, rec)
    written = 0
    for doc_id, entry, write, _ in plan:
        if write:
            write_manifest(entry["new"])
            entry["manifest_written"] = True
            written += 1
    rec["phase"] = "cleanup"
    rec["updated_at"] = _now()
    atomic_write_json(path, rec)

    # 阶段二:删除「旧 − 新」里当前版本已不在用的键(逐文件标记,便于失败后只补删)
    deleted = 0
    for _, entry, _, delete_keys in plan:
        left = [k for k in delete_keys if k not in (entry.get("cleaned_keys") or [])]
        if left:
            deleted += cache.delete(left)
            entry["cleaned_keys"] = sorted((entry.get("cleaned_keys") or []) + left)
        entry["applied"] = True
    rec.update({"status": "applied", "phase": "done", "updated_at": _now(),
                "deleted_keys": int(rec.get("deleted_keys") or 0) + deleted})
    atomic_write_json(path, rec)
    retained = sum(len([k for k in (e.get("stale_keys") or [])
                        if k not in (e.get("cleaned_keys") or [])])
                   for _, e, write, _ in plan if not write)
    logger.info("缓存清理收尾:文件 %d 个(补写清单 %d),本次删除旧键 %d,因仍在使用保留 %d",
                len(plan), written, deleted, retained)
    return {"status": "applied", "files": len(files), "deleted_keys": deleted,
            "retained_keys": retained, "manifests_written": written}


def drop_publish_record(job_id: str) -> None:
    """任务文件被裁剪时顺手删掉已执行完的清单记录;还没收尾的(publishing / prepared)留着。"""
    path = publish_record_path(job_id)
    rec = read_json(path, default=None)
    if not isinstance(rec, dict) or rec.get("status") != "applied":
        return
    try:
        path.unlink()
    except OSError:
        pass


def retry_pending_publishes(active_collection: str | None = None) -> int:
    """启动对账:重试没走完的发布清理(只重试写清单与删键,不重新 OCR)。

    只处理 publishing 记录,以及「prepared 但版本已是当前生效版本」的记录(发布成功、
    翻状态前进程退出);其余 prepared 记录说明本次根本没发布,原样留着不执行。
    """
    d = _records_dir("manifests")
    if not d.is_dir():
        return 0
    n = 0
    for p in sorted(d.glob("*.json")):
        rec = read_json(p, default=None)
        if not isinstance(rec, dict) or rec.get("status") not in ("publishing", "prepared"):
            continue
        try:
            out = apply_publish(str(rec.get("job_id") or ""), active_collection=active_collection)
        except CacheError as exc:
            logger.warning("发布清理重试失败(保留记录 %s):%s", p.name, exc)
            continue
        if out.get("status") == "applied":
            n += 1
    return n


# ---- 删除文件的恢复记录与分阶段清理(§8)----

def prepare_deletion(key: str) -> dict | None:
    """删除**原件之前**调用:把恢复记录与登记标记落盘,返回记录;未登记 / 缓存关闭返回 None。

    这里要把后面几步需要的信息一次读齐(登记、清单快照、待删缓存键),读不到就抛
    CacheError —— 调用方必须据此中止,绝不能先删原件再补记录:否则 Redis 故障时会留下
    「原件已删、却没有任何恢复凭据」的缺口(§8)。

    记录里 phases 明确列出四个阶段:object_deleted / vectors_deleted / cache_cleaned /
    registry_cleaned,由调用方与恢复流程逐步推进。
    """
    cache = get_cache()
    if not cache.enabled:
        return None
    k = _norm_key(key)
    if not k:
        return None
    doc_id = get_id(k)                                    # Redis 读失败 → CacheError,不删原件
    if not doc_id:
        return None
    stored = load_json(cache.get(record_key(doc_id)), what="文件登记")
    manifest = manifest_of(doc_id)                        # 同上:读失败直接抛,不当成没有
    keys = sorted(cache_keys_of(manifest))
    if isinstance(stored, dict):                          # 标记:该身份正在删除,重传不再复用
        stored.update({"status": "deleting", "updated_at": _now()})
        cache.set(record_key(doc_id), json.dumps(stored, ensure_ascii=False))
    rec = {
        "schema_version": SCHEMA_VERSION, "document_id": doc_id, "oss_key": k,
        "manifest": manifest, "cache_keys": keys,
        "registry": {"created_at": (stored or {}).get("created_at", ""),
                     "content_sha256": (stored or {}).get("content_sha256", "")},
        "phases": {"object_deleted": False, "vectors_deleted": False,
                   "cache_cleaned": False, "registry_cleaned": False},
        "created_at": _now(), "updated_at": _now(),
    }
    atomic_write_json(deletion_record_path(doc_id), rec)  # 先落盘,调用方再删原件
    return rec


def abort_deletion(pending: dict | None) -> None:
    """原件删除失败:撤销预置的删除记录与 deleting 标记,让文件保持完整可用。"""
    if not pending:
        return
    doc_id = str(pending.get("document_id") or "")
    if not doc_id:
        return
    try:
        deletion_record_path(doc_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("撤销删除记录失败(下次启动对账会把它当作已受理的删除执行):%s", exc)
    try:
        cache = get_cache()
        stored = load_json(cache.get(record_key(doc_id)), what="文件登记") if cache.enabled else None
        if isinstance(stored, dict) and stored.get("status") == "deleting":
            stored.update({"status": "active", "updated_at": _now()})
            cache.set(record_key(doc_id), json.dumps(stored, ensure_ascii=False))
    except CacheError as exc:
        logger.warning("撤销删除标记失败(该路径下次登记会按新文件处理):%s", exc)


def mark_deletion(pending: dict | None, **phases: bool) -> None:
    """推进恢复记录里的阶段标记(本地 JSON)。失败只告警:阶段可从现场重算,操作可重入。"""
    if not pending:
        return
    doc_id = str(pending.get("document_id") or "")
    if not doc_id:
        return
    path = deletion_record_path(doc_id)
    rec = read_json(path, default=None)
    if not isinstance(rec, dict):                         # 记录已不在(恢复已完成/被撤销):
        logger.warning("删除阶段标记跳过(记录不存在):%s", path.name)  # 不凭内存重建
        return
    rec.setdefault("phases", {}).update({k: bool(v) for k, v in phases.items()})
    rec["updated_at"] = _now()
    try:
        atomic_write_json(path, rec)
    except OSError as exc:
        logger.warning("删除阶段标记写入失败(恢复时会按现场重算):%s", exc)


def forget_file(key: str) -> dict:
    """原件与索引都已删除后调用:落记录 → 清缓存 → 清登记 / 清单 / 路径映射。

    中途失败记录留在 deletions/ 供启动对账重试(只补没做完的阶段,不重跑 OCR)。
    返回 {"document_id", "deleted_keys", "cache_keys", "skipped"}。
    """
    pending = prepare_deletion(key)
    if pending is None:
        return {"document_id": None, "deleted_keys": 0, "cache_keys": 0,
                "skipped": "缓存已关闭" if not get_cache().enabled else "未登记"}
    mark_deletion(pending, object_deleted=True, vectors_deleted=True)   # 调用方保证已删
    return _run_deletion(deletion_record_path(pending["document_id"]))


def finish_deletion(pending: dict | None) -> dict:
    """执行 / 重试删除清理(调用方已推进过阶段标记)。"""
    if pending is None:
        return {"document_id": None, "deleted_keys": 0, "cache_keys": 0, "skipped": "未登记"}
    return _run_deletion(deletion_record_path(pending["document_id"]))


def _snapshot_keys(rec: dict) -> list[str]:
    """本次删除要清的缓存键:只用删除时的快照,绝不重读当前清单(§8:不猜范围)。"""
    keys = rec.get("cache_keys")
    if isinstance(keys, list) and keys:
        return sorted({k for k in keys if k})
    manifest = rec.get("manifest")                        # 兼容旧格式记录(只有清单快照)
    return sorted(cache_keys_of(manifest))


def _cleanup_registry(cache: CacheBackend, rec: dict) -> None:
    """清登记、清单与路径映射 —— 只清仍属于这次删除的部分,不误伤同路径重传的新文件(§8)。"""
    doc_id = str(rec.get("document_id") or "")
    oss_key = str(rec.get("oss_key") or "")
    registry = rec.get("registry") or {}
    snapshot = rec.get("manifest")

    if oss_key and cache.get(path_key(oss_key)) == doc_id:
        cache.delete([path_key(oss_key)])                 # 已指向新 document_id 则不动
    stored = load_json(cache.get(record_key(doc_id)), what="文件登记")
    if isinstance(stored, dict):
        born = registry.get("created_at") or ""
        if not born or stored.get("created_at") == born:  # 同一次登记才删(重传会换 created_at)
            cache.delete([record_key(doc_id)])

    raw = cache.get(manifest_key(doc_id))
    cur = load_json(raw, what="文件缓存清单")
    if cur is None:
        return
    if (isinstance(cur, dict) and isinstance(snapshot, dict)
            and cur.get("content_sha256") == snapshot.get("content_sha256")
            and cur.get("index_version") == snapshot.get("index_version")):
        cache.delete([manifest_key(doc_id)])
    else:
        logger.warning("清单已被更新的发布替换,保留不删(避免删掉现版本仍在用的清单):%s", doc_id)


def _run_deletion(path: Path) -> dict:
    """执行 / 重试一次删除:按 phases 补做没完成的阶段,每步落盘,全程可重入。

    阶段:原件(仅在记录说还没删时补删)→ 索引向量 → 缓存 → 登记 / 清单 /
    路径映射。路径操作先校验登记身份与内容版本，冲突或向量删除失败时保留记录。
    缓存清理只用删除时的清单快照;清单确实不存在就不删任何缓存键。
    """
    rec = read_json(path, default=None)
    if not isinstance(rec, dict):
        return {"document_id": None, "deleted_keys": 0, "cache_keys": 0, "skipped": "记录不可读"}
    doc_id = str(rec.get("document_id") or "")
    if not doc_id:
        return {"document_id": None, "deleted_keys": 0, "cache_keys": 0, "skipped": "记录缺 document_id"}
    phases = rec.get("phases")
    if not isinstance(phases, dict):
        # 旧格式记录(先删原件、后落记录):原件与向量按已删处理,只补缓存与登记两个阶段
        phases = {"object_deleted": True, "vectors_deleted": True,
                  "cache_cleaned": False, "registry_cleaned": False}
        rec["phases"] = phases
    oss_key = str(rec.get("oss_key") or "")

    # 未完成的路径操作只能作用于原身份。路径已复用或身份丢失时保留记录，
    # 不猜测应删除哪个对象，也不按路径删除新文件的向量。
    def verify_owner() -> None:
        if get_id(oss_key) != doc_id:
            raise CacheError(f"删除恢复身份冲突:{oss_key} 已不属于 {doc_id}，已停止删除并保留记录")
        stored = load_json(get_cache().get(record_key(doc_id)), what="文件登记")
        original_hash = (rec.get("registry") or {}).get("content_sha256")
        if original_hash and (not isinstance(stored, dict)
                              or stored.get("content_sha256") != original_hash):
            raise CacheError(f"删除恢复内容版本冲突:{oss_key}，已停止删除并保留记录")

    if not phases.get("object_deleted") and oss_key:      # 阶段 1:原件(请求路径已删过则跳过)
        verify_owner()
        from app.rag import oss                           # 惰性导入:避免模块级循环依赖
        kb = oss.knowledge_store()
        if kb.object_exists(oss_key):
            kb.delete_object(oss_key)
            logger.info("删除恢复:补删原件 %s", oss_key)
        mark_deletion(rec, object_deleted=True)
        phases["object_deleted"] = True

    if not phases.get("vectors_deleted") and oss_key:     # 阶段 2:失败必须保留恢复记录
        verify_owner()
        from app.rag import ingest
        ingest.delete_file_index(oss_key)
        mark_deletion(rec, vectors_deleted=True)

    cache = get_cache()
    keys = _snapshot_keys(rec)                            # 阶段 3:缓存(只用快照,不猜范围)
    deleted = 0
    if not phases.get("cache_cleaned"):
        deleted = cache.delete(keys) if keys else 0
        mark_deletion(rec, cache_cleaned=True)

    if not phases.get("registry_cleaned"):
        _cleanup_registry(cache, rec)                     # 阶段 4:登记 / 清单 / 路径映射
        mark_deletion(rec, registry_cleaned=True)
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("删除恢复记录清理失败(留待下次重试):%s", exc)
    logger.info("文件删除清理完成:%s(删 %d/%d 键)", doc_id, deleted, len(keys))
    return {"document_id": doc_id, "deleted_keys": deleted, "cache_keys": len(keys), "skipped": ""}


def retry_pending_deletions() -> int:
    """启动对账:补完没走完的删除(补删原件 / 向量 / 缓存 / 登记,不重跑 OCR)。"""
    d = _records_dir("deletions")
    if not d.is_dir():
        return 0
    n = 0
    for p in sorted(d.glob("*.json")):
        try:
            _run_deletion(p)
            n += 1
        except CacheError as exc:
            logger.warning("删除恢复失败(保留记录 %s):%s", p.name, exc)
        except Exception as exc:  # noqa: BLE001 —— OSS / Qdrant 未接通也保留记录,下次再试
            logger.warning("删除恢复暂不可执行(保留记录 %s):%s", p.name, exc)
    return n
