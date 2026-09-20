"""RAG 摄取管线:把 OSS 原件抽成文本、切块、向量化,写进 Qdrant。

流程(admin reindex 触发):
  遍历 OSS 前缀 → 逐文件下载 → extract 抽文本
  →(needs_ocr 且引擎已接入才 OCR,否则跳过并计入告警)
  → 切块 → parse_metadata 富化 payload → 分批 embedding(≤10/请求)→ upsert。

v1 是「清空 + 全量重建」:先 recreate_collection 再全量写入;增量(按 hash/mtime
只重嵌变更、删除的删向量)后置。摄取对单个坏文件优雅降级(跳过并记原因),不中断整批
——同 extract / OCR 的降级风格。

顺序上先验依赖(OSS 通 + embeddings 已配)再清库,避免清空后才发现依赖缺失。

payload 约定(检索与引用来源都读它):
  text        切块正文(命中后展示 / 引用摘要)
  oss_key     KB 相对 key(签名 URL 指回原件用)
  source      文件名(展示名)
  category    该文件所在 OSS 前缀(= 用户手建的分类节点,权威分类)
  chunk_index 该文件内的切块序号
  ext         扩展名
  外加 parse_metadata 富化的 project/person/fee_category/spent/budget/
  invoice_amount/doc_type(解析不出的字段不塞)。
"""

from __future__ import annotations

import logging
import uuid

from app.rag import oss, store
from app.rag.embeddings import get_embeddings
from app.rag.extract import ExtractResult, extract
from app.rag.metadata import parse_metadata
from app.rag.ocr import OCRNotConfigured, ocr_available, ocr_image

logger = logging.getLogger(__name__)

_CHUNK_CHARS = 1000       # 单块目标字符数(中文约等于 token 数,远低于 8192 上限)
_CHUNK_OVERLAP = 150      # 硬切超长块时的重叠字符数,避免跨块语义断裂
_UPSERT_BATCH = 128       # 每批 upsert 的向量点数
_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg"})
SKIP_CAP = 200            # 响应里 skipped 明细的上限(避免超大响应)

# 固定命名空间:point id = uuid5(NS, "<oss_key>#<chunk_index>"),重建后 id 稳定,
# 便于将来做增量(按 key 定位 / 覆盖 / 删除)。纯确定性,无随机。
_ID_NS = uuid.uuid5(uuid.NAMESPACE_URL, "qa-knowledge-point")


def _pack(units: list[str], size: int = _CHUNK_CHARS, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """把自然块(页/段/表行)贪心打包成 ≤size 的切块;超长单块按字符硬切(带重叠)。"""
    chunks: list[str] = []
    buf = ""
    for u in units:
        u = (u or "").strip()
        if not u:
            continue
        if len(u) >= size:
            if buf:
                chunks.append(buf)
                buf = ""
            step = max(1, size - overlap)
            for i in range(0, len(u), step):
                piece = u[i : i + size]
                if piece:
                    chunks.append(piece)
            continue
        if buf and len(buf) + 1 + len(u) > size:
            chunks.append(buf)
            buf = u
        else:
            buf = f"{buf}\n{u}" if buf else u
    if buf:
        chunks.append(buf)
    return chunks


def _category_of(key: str) -> str:
    """该文件所在的 OSS 前缀(= 用户手建的分类节点);根下文件为空串。"""
    return key.rsplit("/", 1)[0] + "/" if "/" in key else ""


def _resolve_text(res: ExtractResult, data: bytes) -> tuple[str | None, str | None]:
    """把 extract 结果解析成可切块的正文;返回 (text, skip_reason)。

    text 为 None 表示应跳过该文件,skip_reason 说明原因(不抛,交上层计入告警)。
    """
    if res.error:
        return None, res.error
    if res.needs_ocr:
        if not ocr_available():
            return None, "needs_ocr:OCR 未接入(2d),跳过"
        if res.ext not in _IMAGE_EXTS:
            # 扫描 PDF 需先渲染成图再逐页 OCR,那步留到 2d;此刻只能处理单张图片。
            return None, "needs_ocr:扫描 PDF 待 2d 渲染后再 OCR,跳过"
        try:
            text = (ocr_image(data) or "").strip()
        except OCRNotConfigured as exc:
            return None, f"ocr_unavailable:{exc}"
        except Exception as exc:  # OCR 引擎运行时错:跳过该文件,不中断整批
            return None, f"ocr_failed:{exc}"
        return (text, None) if text else (None, "ocr_empty:OCR 未识别出文本")
    text = (res.text or "").strip()
    return (text, None) if text else (None, "empty:抽取正文为空")


def _file_points(key: str, res: ExtractResult, text: str) -> tuple[list[str], list[dict], list[str]]:
    """把单个文件的正文切块 → (texts, payloads, ids)。

    point id = 确定性 uuid5(key#序号),重建 / 增量都稳定;payload 按 ingest 顶部约定组装。
    切块后为空则返回三个空列表(上层据此计入跳过)。全量重建与增量索引共用此逻辑。
    """
    chunks = _pack(res.blocks or [text])
    if not chunks:
        return [], [], []
    meta = parse_metadata(key)
    source = key.rsplit("/", 1)[-1]
    category = _category_of(key)
    texts: list[str] = []
    payloads: list[dict] = []
    ids: list[str] = []
    for i, ch in enumerate(chunks):
        payload = {
            "text": ch,
            "oss_key": key,
            "source": source,
            "category": category,
            "chunk_index": i,
            "ext": res.ext,
        }
        payload.update(meta)
        payloads.append(payload)
        texts.append(ch)
        ids.append(str(uuid.uuid5(_ID_NS, f"{key}#{i}")))
    return texts, payloads, ids


def index_file(key: str) -> dict:
    """增量索引单个原件:删旧点 → 抽取 → 切块 → 向量化 → 写 Qdrant。返回结果 dict。

    幂等:先按 key 删旧向量再写,重复上传 / 块数变化都不残留。needs_ocr / unsupported /
    空正文 → 不入库,indexed=False + reason。Embeddings / Qdrant 未配置 → 抛 RuntimeError
    (交路由转 503);单文件运行时异常不抛(记 reason 返回),与全量摄取的降级风格一致。
    """
    embeddings = get_embeddings()   # 先验 embeddings 已配(未配抛 RuntimeError)
    store.delete_by_oss_key(key)    # 幂等:清掉该文件旧向量(顺带验 Qdrant 通)
    try:
        data = oss.get_object(key)
        res = extract(data, key)
        text, reason = _resolve_text(res, data)
        if text is None:
            return {"key": key, "indexed": False, "chunks": 0, "reason": reason}
        texts, payloads, ids = _file_points(key, res, text)
        if not texts:
            return {"key": key, "indexed": False, "chunks": 0, "reason": "empty:切块后为空"}
        vectors = embeddings.embed_documents(texts)
        points = [store.make_point(ids[j], vectors[j], payloads[j]) for j in range(len(texts))]
        upserted = 0
        for b in range(0, len(points), _UPSERT_BATCH):
            upserted += store.upsert(points[b : b + _UPSERT_BATCH])
        return {"key": key, "indexed": True, "chunks": upserted, "reason": None}
    except Exception as exc:  # 下载 / 抽取 / 向量化 / 写库的运行时错:如实返回,不抛
        logger.warning("单文件索引失败 %s:%s", key, exc)
        return {"key": key, "indexed": False, "chunks": 0, "reason": f"error:{exc}"}


def delete_file_index(key: str) -> int:
    """删除某原件的全部向量(删原件时连带清索引)。返回删除的向量数。"""
    return store.delete_by_oss_key(key)


def reindex(prefix: str = "") -> dict:
    """清空并全量重建向量索引(v1)。prefix 非空则只重建该子树。返回统计 dict。

    任一依赖(OSS / Qdrant / Embeddings)未配置会抛 RuntimeError,交路由转 503。
    """
    files = oss.list_all(prefix)      # 先验 OSS 通 + 拿文件清单
    embeddings = get_embeddings()     # 先验 embeddings 已配(fail-fast,别清库后才发现)
    name = store.recreate_collection()  # 清空并重建 collection

    texts: list[str] = []
    payloads: list[dict] = []
    ids: list[str] = []
    skipped: list[dict] = []
    indexed = 0

    for f in files:
        key = f["key"]
        try:
            data = oss.get_object(key)
            res = extract(data, key)
            text, reason = _resolve_text(res, data)
            if text is None:
                skipped.append({"key": key, "reason": reason})
                continue
            f_texts, f_payloads, f_ids = _file_points(key, res, text)
            if not f_texts:
                skipped.append({"key": key, "reason": "empty:切块后为空"})
                continue
            texts.extend(f_texts)
            payloads.extend(f_payloads)
            ids.extend(f_ids)
            indexed += 1
        except Exception as exc:  # 单文件任何异常都不中断整批
            logger.warning("摄取跳过 %s:%s", key, exc)
            skipped.append({"key": key, "reason": f"error:{exc}"})

    upserted = 0
    if texts:
        logger.info("摄取:%d 文件 → %d 切块,开始向量化(≤10/请求)…", indexed, len(texts))
        vectors = embeddings.embed_documents(texts)
        points = [store.make_point(ids[j], vectors[j], payloads[j]) for j in range(len(texts))]
        for b in range(0, len(points), _UPSERT_BATCH):
            upserted += store.upsert(points[b : b + _UPSERT_BATCH])

    result = {
        "collection": name,
        "prefix": prefix,
        "total_files": len(files),
        "indexed_files": indexed,
        "skipped_files": len(skipped),
        "chunks": len(texts),
        "vectors": upserted,
        "skipped": skipped[:SKIP_CAP],
        "skipped_truncated": len(skipped) > SKIP_CAP,
    }
    logger.info("摄取完成:%s", {k: v for k, v in result.items() if k != "skipped"})
    return result
