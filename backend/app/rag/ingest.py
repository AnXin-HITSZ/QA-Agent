"""RAG 摄取管线:把 OSS 原件抽成文本、切块、向量化,写进 Qdrant —— 带安全发布。

流程(唯一的索引入口 run_index_job;单文件重试 = keys 范围的同一条流程):
  固定源文件清单(含大小与修改时间快照)→ 建候选版本集合 → 复制范围外旧向量
  → 逐文件:下载 → 逐页提取(document_extract)→ 切块 → 向量化 → 写入候选集合
  → 校验源文件未变 / 数量对得上 → 翻转 manifest 指针发布;任何目标文件失败则不发布,
  旧版本继续可检索(见 store.py 顶部说明)。

要点:
- 不先删旧索引:旧集合在发布前一直是生效版本,发布失败时旧版本仍然可检索。
- 子树 / 单文件任务把范围外向量原样复制进候选集合,范围外内容不会丢;本次范围内
  的目标文件则整份重建,旧的多余切块不会残留(候选集合里根本没有它们)。
- 源文件在准备后被改动 → 拒绝发布并提示重新排队(比对大小与修改时间快照)。
- 失败 = 本该有内容却没抽全(页面识别失败 / 部分页无文本):任一目标文件失败,本次就不发布,
  旧版本继续可检索;重跑很便宜(已识别页面命中缓存)。按当前方式本就抽不出内容的文件
  (扫描件在 native_only 下、不支持的格式、坏文件)只算「跳过」—— 照常发布,明细里给出原因,
  不让一颗坏文件卡住整个知识库。

payload 约定(检索与引用来源都读它):
  text                切块正文(命中后展示 / 引用摘要)
  oss_key             KB 相对 key(签名 URL 指回原件用)
  source              文件名(展示名)
  category            该文件所在 OSS 前缀(= 用户手建的分类节点,权威分类)
  chunk_index         该文件内的切块序号
  ext                 扩展名
  content_sha256      原件内容哈希(判断"原件是否变过")
  page_numbers        该块覆盖的原件页码(列表,从 1 起;Office 类为空)
  extraction_methods  实际使用的提取方法(native / ocr)
  extraction_version  提取策略版本
  index_version       写入时的索引版本(物理集合名)
  recognized_fields   该块是否来自专用接口的结构化字段
  外加 parse_metadata 富化的 project/person/fee_category/spent/budget/
  invoice_amount/doc_type(解析不出的字段不塞)。
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field

from app.config import get_settings
from app.rag import documents, embedding_cache, oss, store
from app.rag.cache_store import CacheError, ensure_available
from app.rag.document_extract import (
    NATIVE_ONLY, DocumentResult, TextUnit, effective_mode, extract_document, incomplete_reason,
)
from app.rag.embeddings import get_embeddings
from app.rag.metadata import parse_metadata

logger = logging.getLogger(__name__)

_CHUNK_CHARS = 1000       # 单块目标字符数(中文约等于 token 数,远低于 8192 上限)
_CHUNK_OVERLAP = 150      # 硬切超长块时的重叠字符数,避免跨块语义断裂
_UPSERT_BATCH = 128       # 每批 upsert 的向量点数
SKIP_CAP = 200            # 响应里 skipped 明细的上限(避免超大响应)
FILE_CAP = 5000           # 任务文件明细上限(超出截断并标记)
_FAILED_PAGES_CAP = 20

_ID_NS = uuid.uuid5(uuid.NAMESPACE_URL, "qa-knowledge-point")

# 可重试的失败特征(超时 / 网络 / 限流 / 5xx):命中则整份文件再试一轮。
_TRANSIENT_MARKERS = (
    "NetworkTimeout", "NetworkError", "Throttling", "ServiceUnavailable",
    "InternalError", "ServerError", "Timeout", "BadResponse: 响应不是 JSON",
)


@dataclass(frozen=True)
class Options:
    """一次索引请求的提取选项。默认按原生提取,不隐式产生付费调用。"""

    extraction_mode: str = NATIVE_ONLY
    mixed_invoice: bool = False
    refresh_ocr: bool = False

    @property
    def mode(self) -> str:
        """展开成 document_extract 的内部方式(invoice + 勾选 → mixed_invoice)。"""
        return effective_mode(self.extraction_mode, self.mixed_invoice)

    def as_dict(self) -> dict:
        return {"extraction_mode": self.extraction_mode, "mixed_invoice": self.mixed_invoice,
                "refresh_ocr": self.refresh_ocr}


@dataclass(frozen=True)
class Scope:
    """本次任务的范围:整个前缀子树,或一份显式 key 清单(两者互斥)。"""

    kind: str = "prefix"          # prefix / keys
    prefix: str = ""
    keys: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"kind": self.kind, "prefix": self.prefix,
                "keys": list(self.keys)} if self.kind == "keys" else {"kind": self.kind, "prefix": self.prefix}

    def covers(self, key: str) -> bool:
        if self.kind == "keys":
            return key in self.keys
        return key.startswith(self.prefix)


@dataclass
class Chunk:
    text: str
    pages: tuple[int, ...] = ()
    from_fields: bool = False


@dataclass
class Prepared:
    """一份原件准备结果(下载 + 提取 + 切块,尚未向量化)。"""

    key: str
    status: str = "indexed"        # indexed / skipped / failed
    reason: str | None = None
    chunks: list[Chunk] = field(default_factory=list)
    content_sha256: str = ""
    document_id: str = ""          # 文件身份(§6);缓存关闭时为空字符串
    page_keys: list[tuple[int, str | None, str | None]] = field(default_factory=list)
    ext: str = ""
    method: str = ""
    pages_total: int = 0
    pages_ok: int = 0
    pages_blank: int = 0
    pages_failed: int = 0
    pages_cached: int = 0
    failed_pages: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def pages(self) -> dict:
        return {"total": self.pages_total, "ok": self.pages_ok, "blank": self.pages_blank,
                "failed": self.pages_failed, "cached": self.pages_cached}

    def detail(self) -> dict:
        return {"key": self.key, "status": self.status, "chunks": len(self.chunks),
                "reason": self.reason, "method": self.method, "ext": self.ext,
                "pages": self.pages, "failed_pages": self.failed_pages[:20],
                "warnings": self.warnings[:5]}


# ---- 切块 ----

def _pack_units(units: list[TextUnit], size: int = _CHUNK_CHARS,
                overlap: int = _CHUNK_OVERLAP) -> list[Chunk]:
    """把自然单元(页 / 段)打成 ≤size 的切块,并保留每块覆盖的页码。

    单块超长时按字符硬切(带重叠);此时页码取该单元自己的页码,不跨页乱标。
    """
    chunks: list[Chunk] = []
    buf = ""
    pages: set[int] = set()
    fields = False

    def flush() -> None:
        nonlocal buf, pages, fields
        if buf:
            chunks.append(Chunk(buf, tuple(sorted(pages)), fields))
        buf, pages, fields = "", set(), False

    for u in units:
        text = (u.text or "").strip()
        if not text:
            continue
        if len(text) >= size:
            flush()
            step = max(1, size - overlap)
            for i in range(0, len(text), step):
                piece = text[i: i + size]
                if piece:
                    chunks.append(Chunk(piece, (u.page,) if u.page else (), u.from_fields))
            continue
        if buf and len(buf) + 1 + len(text) > size:
            flush()
        buf = f"{buf}\n{text}" if buf else text
        if u.page:
            pages.add(u.page)
        fields = fields or u.from_fields
    flush()
    return chunks


def _category_of(key: str) -> str:
    """该文件所在的 OSS 前缀(= 用户手建的分类节点);根下文件为空串。"""
    return key.rsplit("/", 1)[0] + "/" if "/" in key else ""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_points(key: str, chunks: list[Chunk], *, content_sha256: str, ext: str,
                 method: str, index_version: str, extraction_version: str) -> tuple[list[str], list[dict], list[str]]:
    """切块 → (texts, payloads, ids)。point id = 确定性 uuid5(key#序号),重建前后稳定。"""
    meta = parse_metadata(key)
    source = key.rsplit("/", 1)[-1]
    category = _category_of(key)
    texts, payloads, ids = [], [], []
    for i, ch in enumerate(chunks):
        payload = {
            "text": ch.text,
            "oss_key": key,
            "source": source,
            "category": category,
            "chunk_index": i,
            "ext": ext,
            "content_sha256": content_sha256,
            "page_numbers": list(ch.pages),
            "extraction_methods": method,
            "extraction_version": extraction_version,
            "index_version": index_version,
            "recognized_fields": bool(ch.from_fields),
        }
        payload.update(meta)
        payloads.append(payload)
        texts.append(ch.text)
        ids.append(str(uuid.uuid5(_ID_NS, f"{key}#{i}")))
    return texts, payloads, ids


# ---- 单文件准备 ----

def _is_failure(res: DocumentResult) -> bool:
    """是否算「目标文件失败」(会让本次不发布)。

    只有"本来该有内容却没抽全"才算失败:页面识别失败 / 部分页无文本 —— 这类问题
    重跑(或换一种提取方式)就能修好,发布出去等于悄悄丢内容,所以宁可不发布。

    其余一律算"跳过我",照常发布,避免一颗坏文件卡住整个知识库:
    - 无法解析 / 不支持格式 / 空文件(本来就没有可检索内容,与旧索引一致);
    - native_only 下没有文本层的扫描件(是用户没选 OCR 方式,不是失败)。
    """
    if res.error or res.needs_ocr:
        return False
    return res.incomplete


def _reason_of(res: DocumentResult) -> str:
    """提取结果 → 不索引的原因(给用户看的一句话)。"""
    if res.error:
        return res.error
    if res.needs_ocr:
        return "needs_ocr:该文件没有文本层,请选择一种 OCR 提取方式"
    if res.incomplete:
        return incomplete_reason(res) or "incomplete:提取不完整"
    if not res.text.strip():
        return "empty:抽取正文为空"
    return ""


def prepare_file(kb, key: str, opts: Options) -> Prepared:
    """下载 → 登记身份 → 逐页提取 → 切块。不抛业务异常,失败原因写进 Prepared.reason。

    缓存错误(CacheError)照抛:那是「整批停下来」的信号,不是单个文件的失败(§9)。
    """
    prep = Prepared(key=key, ext=(key.rsplit(".", 1)[-1].lower() if "." in key else ""))
    try:
        data = kb.get_object(key)
    except Exception as exc:
        prep.status, prep.reason = "failed", f"download_failed:{exc}"
        return prep
    prep.content_sha256 = _sha256(data)
    prep.document_id = documents.register(key, content_sha256=prep.content_sha256)
    try:
        res = extract_document(data, key, opts.mode, refresh=opts.refresh_ocr)
    except CacheError:
        raise
    except Exception as exc:
        prep.status, prep.reason = "failed", f"extract_failed:{type(exc).__name__}:{exc}"
        return prep
    prep.page_keys = [(p.page_number, p.ocr_cache_key, p.text_cache_key) for p in res.pages]

    prep.method = ",".join(res.methods) or ("ocr" if opts.mode != NATIVE_ONLY else "native")
    prep.ext = res.ext or prep.ext
    prep.pages_total = len(res.pages)
    prep.pages_ok = sum(1 for p in res.pages if p.status == "success")
    prep.pages_blank = sum(1 for p in res.pages if p.status == "blank")
    prep.pages_failed = sum(1 for p in res.pages if p.status in ("incomplete", "failed"))
    prep.pages_cached = sum(1 for p in res.pages if p.cached)
    prep.failed_pages = [p.page_number for p in res.pages if p.status in ("incomplete", "failed")]
    prep.warnings = list(res.warnings)[:5]

    reason = _reason_of(res)
    if reason:
        prep.status = "failed" if _is_failure(res) else "skipped"
        prep.reason = reason
        return prep
    chunks = _pack_units(res.units)
    if not chunks:
        prep.status, prep.reason = "skipped", "empty:切块后为空"
        return prep
    prep.chunks = chunks
    return prep


# ---- 范围与源快照 ----

def scope_files(kb, scope: Scope) -> tuple[list[dict], list[dict]]:
    """固定本次源文件清单:返回 (范围内的文件, 范围外的文件)。

    用一次列举同时拿到 size / last_modified 快照,发布前用它核对源文件有没有被改动。
    """
    all_files = kb.list_all("")
    snapshot = {f["key"]: f for f in all_files}
    if scope.kind == "keys":
        targets, missing = [], []
        for k in scope.keys:
            f = snapshot.get(k)
            if f is None:
                missing.append({"key": k, "reason": "not_found:OSS 上已不存在"})
            else:
                targets.append(f)
        return targets, missing
    targets = [f for f in all_files if scope.covers(f["key"])]
    return targets, []


def source_changed(kb, expected: list[dict]) -> list[str]:
    """发布前核对:本次目标文件是否在上次准备后被改动(比对大小 + 修改时间)。

    不比对内容哈希(那要重新下载全部原件);OSS 的 last_modified 为秒级,同一秒内
    覆盖且大小不变才会漏判 —— 概率极低且下一轮任务必然发现,文档中已注明。
    """
    now = {f["key"]: f for f in kb.list_all("")}
    changed = []
    for f in expected:
        cur = now.get(f["key"])
        if cur is None:
            changed.append(f"{f['key']}(已删除)")
        elif cur.get("size") != f.get("size") or cur.get("last_modified") != f.get("last_modified"):
            changed.append(f"{f['key']}(内容已变化)")
    return changed


# ---- 主编排 ----

def run_index_job(kb, scope: Scope, opts: Options, *, progress=None, retries: int | None = None,
                  job_id: str | None = None) -> dict:
    """一次索引任务:准备 → 写入候选集合 → 校验 → 发布 → 更新清单并清理旧缓存。

    progress(done, total, detail) 每处理完一个文件回调一次;detail 为该文件的结果。
    依赖(OSS / Qdrant / Embeddings / 缓存)未配置会抛 RuntimeError,交上层转 503。
    单文件失败不中断整批,但会导致本次不发布(旧索引保持可用);缓存不可用 / 内存上限
    则立刻停在当前文件并如实报错(§9),不继续跑完整批。
    """
    targets, missing = scope_files(kb, scope)   # 先验 OSS 通 + 固定清单
    embeddings = get_embeddings()               # 先验 embeddings 已配(fail-fast)
    ensure_available()                          # 再验缓存可用(fail-fast,§9)
    store.ensure_collection()                   # 首次部署:先有一个空的生效集合可复制
    version = store.create_staging()
    record_id = job_id or version               # 清单阶段记录的标识
    logger.info("索引任务开始:范围 %s → 候选集合 %s(目标 %d 个文件,提取方式 %s)",
                scope.as_dict(), version, len(targets), opts.mode)

    copied_keys: set[str] = set()
    manifests: dict[str, dict] = {}             # 待发布清单:document_id → 清单(§7)

    def keep(payload: dict) -> bool:
        k = payload.get("oss_key") or ""
        if scope.covers(k):
            return False                 # 范围内:整份重建,旧点一个不留
        copied_keys.add(k)
        return True

    copied, dropped = store.copy_points(store.active_collection(), version, keep)

    details: list[dict] = []
    totals = {"total_files": len(targets) + len(missing), "indexed_files": 0,
              "skipped_files": len(missing), "failed_files": 0, "chunks": 0, "vectors": 0}
    pages = {"total": 0, "ok": 0, "blank": 0, "failed": 0, "cached": 0}
    wait = get_settings().ocr_max_retries if retries is None else retries

    for m in missing:
        details.append({"key": m["key"], "status": "skipped", "chunks": 0, "reason": m["reason"],
                        "method": "", "ext": "", "pages": dict.fromkeys(pages, 0),
                        "failed_pages": [], "warnings": []})

    cache_failure: CacheError | None = None
    for i, f in enumerate(targets, 1):
        key = f["key"]
        try:
            prep = prepare_file(kb, key, opts)
            # 瞬时故障(超时 / 限流 / 5xx):整份文件再试一轮;OCR 内部已有单页重试,这里只兜底。
            if prep.status == "failed" and wait > 0 and any(m in (prep.reason or "") for m in _TRANSIENT_MARKERS):
                logger.warning("文件 %s 遇到瞬时故障(%s),%d 秒后重试", key, prep.reason, 2)
                time.sleep(2)
                prep = prepare_file(kb, key, opts)

            for k in ("total", "ok", "blank", "failed", "cached"):
                pages[k] += prep.pages[k]
            if prep.status == "indexed":
                texts, payloads, ids = build_points(
                    key, prep.chunks, content_sha256=prep.content_sha256, ext=prep.ext,
                    method=prep.method, index_version=version,
                    extraction_version=f"v{_strategy_version()}",
                )
                try:
                    vectors = embedding_cache.embed_documents(embeddings, texts)
                    points = [store.make_point(ids[j], vectors[j], payloads[j]) for j in range(len(texts))]
                    n = 0
                    for b in range(0, len(points), _UPSERT_BATCH):
                        n += store.upsert(points[b: b + _UPSERT_BATCH], collection=version)
                    totals["indexed_files"] += 1
                    totals["chunks"] += len(texts)
                    totals["vectors"] += n
                    if prep.document_id:      # 命中缓存也要为当前文件生成清单(§7)
                        manifests[prep.document_id] = documents.build_manifest(
                            document_id=prep.document_id, oss_key=key,
                            content_sha256=prep.content_sha256,
                            extraction_mode=opts.extraction_mode, mixed_invoice=opts.mixed_invoice,
                            index_version=version, pages=prep.page_keys,
                            embedding_keys=[embedding_cache.cache_key(t) for t in texts],
                        )
                except CacheError:
                    raise
                except Exception as exc:   # 向量化 / 写库失败:该文件计失败,不发布
                    logger.warning("文件 %s 写入失败:%s", key, exc)
                    prep.status, prep.reason = "failed", f"write_failed:{exc}"
            if prep.status == "failed":
                totals["failed_files"] += 1
            elif prep.status == "skipped":
                totals["skipped_files"] += 1
            details.append(prep.detail())
            if progress:
                progress(i, len(targets), details[-1])
        except CacheError as exc:
            # 缓存不可用 / 内存上限 / 写入失败:停在当前文件,不再继续处理整批(§9)
            logger.error("缓存错误,索引任务暂停于 %s:%s", key, exc)
            cache_failure = exc
            break

    summary = {
        "scope": scope.as_dict(),
        "options": opts.as_dict(),
        "index_version": version,
        "previous": store.active_collection(),
        "published": False,
        "copied_out_of_scope": copied,
        "replaced_in_scope": dropped,
        "files": details[:FILE_CAP],
        "files_truncated": len(details) > FILE_CAP,
        "skipped": [{"key": d["key"], "reason": d["reason"]} for d in details if d.get("reason")][:SKIP_CAP],
        **totals,
        "pages": pages,
        "cache": {},
        "message": "",
    }

    if cache_failure is not None:
        summary["message"] = str(cache_failure)
        summary["cache"] = {"status": "failed", "message": str(cache_failure)}
        store.abort_staging(version)
        return summary

    if totals["failed_files"]:
        summary["message"] = f"有 {totals['failed_files']} 个文件未能完成,本次不发布,旧索引继续可用"
        store.abort_staging(version)
        logger.warning("索引任务未发布:%s", summary["message"])
        return summary

    changed = source_changed(kb, targets)
    if changed:
        summary["message"] = ("准备期间源文件发生变化,本次不发布,请重新排队:" + "; ".join(changed[:5]))
        store.abort_staging(version)
        logger.warning("索引任务未发布:%s", summary["message"])
        return summary

    # 发布前把"复制进来但已被删除"的旧文件剔掉,避免新版本复活已删除内容
    gone = sorted(k for k in copied_keys if not kb.object_exists(k))
    for k in gone:
        store.delete_by_oss_key(k, collection=version)
        summary["copied_out_of_scope"] -= 1
    if gone:
        logger.info("候选集合剔除 %d 个已被删除的旧文件:%s", len(gone), gone[:5])

    # 发布前把待发布清单写进任务记录(§8 步骤 1):发布失败就停在 prepared,正式清单与旧缓存都不动
    try:
        pending = documents.prepare_publish(record_id, version, manifests)
    except OSError as exc:
        summary["message"] = f"待发布清单写入失败,本次不发布:{exc}"
        store.abort_staging(version)
        logger.error("索引任务未发布:%s", summary["message"])
        return summary

    expected = summary["copied_out_of_scope"] + summary["vectors"]
    try:
        store.publish(version, expected_count=expected,
                      note=f"{scope.as_dict()} 提取方式={opts.extraction_mode}")
        summary["published"] = True
        summary["message"] = "已发布"
    except Exception as exc:   # 校验不过:候选集合删除,旧版本原样
        summary["message"] = f"发布校验未通过,已回滚到旧索引:{exc}"
        store.abort_staging(version)
        logger.error("索引发布失败:%s", exc)
        return summary

    # 发布成功 → 写正式清单 → 删除「旧清单键 − 新清单键」(§8 步骤 3–4)
    try:
        summary["cache"] = (
            documents.apply_publish(record_id, active_collection=store.active_collection())
            if pending is not None else {"status": "skipped", "message": "没有需要登记的缓存清单"}
        )
    except CacheError as exc:
        # 已发布的索引不回退:清单与旧缓存清理留待启动对账重试(只删缓存,不重跑 OCR)
        summary["cache"] = {"status": "pending", "message": str(exc)}
        summary["message"] = f"已发布,但缓存清单未更新(旧缓存暂未清理,重启后自动重试):{exc}"
        logger.error("发布后清单更新失败:%s", exc)
    return summary


def _strategy_version() -> str:
    from app.rag.document_extract import EXTRACTION_VERSION
    from app.rag.ocr_cache import TEXT_RULES_VERSION
    return f"{EXTRACTION_VERSION}.{TEXT_RULES_VERSION}"


# ---- 单文件清理 ----

def delete_file_index(key: str) -> int:
    """删除某原件的全部向量(删原件时连带清索引)。返回删除的向量数。"""
    return store.delete_by_oss_key(key)
