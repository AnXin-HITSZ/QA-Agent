"""OCR 两层缓存(Redis String/JSON):识别结果 + 文本转换,分别管理、互不连坐。

  第 1 层 识别缓存   qa:cache:ocr:v1:<digest>
      键 = 实际提交图片字节的 SHA-256 + 供应商 / 接口 / 端点 / Type / 识别配置版本。
      **PDF 页码与整份文件哈希不参与此层内容键** —— 同一张图出现在哪一页、哪个文件都命中同一个键;
      换 DPI / 换 Type / 换端点 / 换配置版本则自然落新键。值 = 有效供应商响应 + 请求 ID / 识别时间。
  第 2 层 文本转换   qa:cache:text:v1:<digest>
      键 = OCR 有效识别内容哈希(不含 RequestId、响应时间)+ 转换规则版本 + 转换选项(ocr_type)。
      修改中文字段标签 / 段落 / 表格表达后只提高 TEXT_RULES_VERSION:从识别缓存重新转换,不调 OCR。

两条写入门槛(方案 §4.1):失败与空结果一律不写成功缓存(异常向上抛,归一化后正文为空
视为「无有效识别内容」);强制刷新失败保留旧值(异常时根本没写)。

返回的 CachedPage 带两个缓存键,供文件清单记录「当前发布版本用了哪些缓存」(§7)。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.rag import ocr
from app.rag.cache_store import CACHE_PREFIX, get_cache, load_json, single_flight, write_with_retry
from app.rag.ocr import OCRPage

logger = logging.getLogger(__name__)

# 识别配置版本:请求参数 / 端点形态 / 供应商响应有实质变化时 +1(只影响第 1 层)。
OCR_CACHE_VERSION = "1"
# 文本转换规则版本:字段标签、段落拼法、表格表达等转换代码变化时 +1(只影响第 2 层)。
TEXT_RULES_VERSION = "1"
# 键格式 / 摘要算法版本:键的结构或摘要输入集合变化时 +1(两层同时失效)。
KEY_VERSION = "v1"

_PROVIDER = "aliyun"


def _digest(payload: dict) -> str:
    """确定性序列化后的完整 SHA-256:参数排序、编码、分隔符必须一致(§4)。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- 键 ----

def ocr_digest(image: bytes, ocr_type: str) -> str:
    """第 1 层摘要:图片字节哈希 + 供应商 / 接口 / 端点 / Type / 配置版本。

    不含密钥、OSS 路径、文件名、任务 ID,也不含页码与整份文件哈希(§4.1)。
    """
    try:
        aliyun_type = ocr.ocr_type_to_aliyun(ocr_type)
    except ValueError:
        aliyun_type = f"unknown:{ocr_type}"
    return _digest({
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "provider": _PROVIDER,
        "action": ocr.API_ACTION,
        "endpoint": ocr.endpoint(),
        "type": aliyun_type,
        "params": {},                 # 当前识别请求没有 Type 之外的业务参数,有则加进来
        "config_version": OCR_CACHE_VERSION,
    })


def text_digest(content_hash: str, ocr_type: str) -> str:
    """第 2 层摘要:有效识别内容哈希 + 转换规则版本 + 转换选项。"""
    return _digest({
        "content_hash": content_hash,
        "rules_version": TEXT_RULES_VERSION,
        "options": {"ocr_type": ocr_type},
    })


def ocr_cache_key(image: bytes, ocr_type: str) -> str:
    return f"{CACHE_PREFIX}ocr:{KEY_VERSION}:{ocr_digest(image, ocr_type)}"


def text_cache_key(content_hash: str, ocr_type: str) -> str:
    return f"{CACHE_PREFIX}text:{KEY_VERSION}:{text_digest(content_hash, ocr_type)}"


def content_hash(raw: dict) -> str:
    """OCR 有效识别内容哈希:只取供应商返回的 Data 子树,不含 RequestId / 响应时间。"""
    return _digest({"data": raw.get("Data")})


# ---- 值 ----

@dataclass(frozen=True)
class CachedPage:
    """一次带缓存的两层识别结果(缓存键供清单记录用)。"""

    page: OCRPage
    ocr_hit: bool            # True = 未调用供应商(第 1 层命中)
    ocr_cache_key: str
    text_cache_key: str


def _read_raw(cache, key: str) -> dict | None:
    """读第 1 层;不存在 / 损坏 / 形态不对都按未命中返回 None(读取失败会抛,不吞)。"""
    payload = load_json(cache.get(key), what="OCR 识别缓存")
    if not isinstance(payload, dict):
        return None
    raw = payload.get("raw")
    return raw if isinstance(raw, dict) else None


def _raw_envelope(raw: dict) -> str:
    return json.dumps({
        "schema": 1,
        "kind": "ocr",
        "format": "aliyun-recognizeAllText-json",
        "config_version": OCR_CACHE_VERSION,
        "request_id": raw.get("RequestId"),
        "recognized_at": _now(),
        "raw": raw,
    }, ensure_ascii=False)


def _page_payload(page: OCRPage, content_hash_value: str = "") -> dict:
    """第 2 层的值:转换后的正文 + 来源标记 + 告警。

    不写死原文件页码与路径(共享结果跨文件复用,来源在建立索引时从清单补);
    不落票据字段明细(字段值已在正文里,不另建可查询字段表,方案 §10)。
    """
    return {
        "schema": 1,
        "kind": "text",
        "rules_version": TEXT_RULES_VERSION,
        "text_scope": "page",
        "content_hash": content_hash_value,
        "ocr_type": page.ocr_type,
        "aliyun_type": page.aliyun_type,
        "text": page.text,
        "recognized_fields": bool(page.recognized_fields or page.fields),
        "subimage_count": page.subimage_count,
        "is_mixed_mode": page.is_mixed_mode,
        "warnings": list(page.warnings),
        "engine": page.engine,
    }


def _page_from_payload(payload: dict) -> OCRPage:
    return OCRPage(
        text=payload.get("text") or "",
        fields={},   # 字段明细不落缓存:正文已含字段值,命中后只保留 recognized_fields 标记
        recognized_fields=bool(payload.get("recognized_fields")),
        ocr_type=payload.get("ocr_type") or "",
        aliyun_type=payload.get("aliyun_type") or "",
        subimage_count=int(payload.get("subimage_count") or 0),
        is_mixed_mode=bool(payload.get("is_mixed_mode")),
        warnings=list(payload.get("warnings") or []),
        engine=payload.get("engine") or _PROVIDER,
        raw={},
    )


def _read_text(cache, key: str) -> dict | None:
    payload = load_json(cache.get(key), what="文本转换缓存")
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        return None
    return payload


# ---- 对外入口 ----

def recognize_cached(image: bytes, ocr_type: str, *, refresh: bool = False) -> CachedPage:
    """一张图片 → 两层缓存后的 OCRPage。测试里替换 ocr.recognize_raw(网络边界)即可离线跑。

    refresh=True 只强制重新识别(跳过第 1 层的读):转换结果仍按内容哈希复用 ——
    「强制刷新」不自动重算相同文本的向量(§5)。刷新失败时旧缓存原样保留,异常向上抛,
    由调用方在本次任务的明细里明确报错(不隐瞒失败)。
    """
    cache = get_cache()   # NullCache(显式关闭缓存)自然退化为「永远未命中」,无需分支
    okey = ocr_cache_key(image, ocr_type)

    raw, ocr_hit = single_flight(
        kind="ocr", identity=ocr_digest(image, ocr_type),
        read=lambda: None if refresh else _read_raw(cache, okey),
        compute=lambda: ocr.recognize_raw(image, ocr_type),
        keep=lambda r: bool(ocr.normalize(r, ocr_type).text.strip()),   # 空内容不算成功(§4.1)
        write=lambda r: write_with_retry(cache, okey, _raw_envelope(r)),
    )

    chash = content_hash(raw)
    tkey = text_cache_key(chash, ocr_type)
    payload, text_hit = single_flight(
        kind="text", identity=text_digest(chash, ocr_type),
        read=lambda: _read_text(cache, tkey),
        compute=lambda: _page_payload(ocr.normalize(raw, ocr_type), chash),
        keep=lambda p: bool(p["text"].strip()),                          # 空结果不写成功缓存
        write=lambda p: write_with_retry(cache, tkey, json.dumps(p, ensure_ascii=False)),
    )
    # 命中转换缓存:字段明细不落缓存(§10),只有 recognized_fields 标记;
    # 本次刚转换的:字段明细还在内存里,原样交给调用方(与未接缓存时行为一致)。
    page = _page_from_payload(payload) if text_hit else ocr.normalize(raw, ocr_type)

    return CachedPage(page=page, ocr_hit=ocr_hit,
                      ocr_cache_key=okey, text_cache_key=tkey)
