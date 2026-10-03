"""OCR:阿里云「OCR 统一识别」(RecognizeAllText)适配器 + 响应归一化。

一次调用 = 一张图(或渲染后的一个 PDF 页);识别类型由调用方显式指定,不做自动分类、
不做按目录前缀的路由 —— 用户在前端选什么类型,这里就调什么 Type。

接口形态(2026-10 核过官方 meta 元数据 + 真实网关验证):
- 端点形如 https://ocr-api.cn-hangzhou.aliyuncs.com/?<参数>,POST,图片字节直接放 HTTP body
  (也可用 Url 参数传图链,本项目一律用字节流,不公开 OSS 对象);
- 必填查询参数 Type,取值 Advanced(通用文字)/Invoice(发票)/MixedInvoice(混贴票据)/
  PaymentRecord(付款详情)/Table/ShoppingReceipt 等;
- 签名走 RPC 风格 SignatureVersion=1.0 (HMAC-SHA1):只签查询参数、不签 body。
  (已用假 AK 打过真实网关:返回 InvalidAccessKeyId.NotFound,说明签名格式被接受。)
- 单请求图片上限 10MB。
- 返回 Data 下是 RecognitionResult:整页文本在 Content;票据类字段在
  SubImages[].KvInfo.Data(字段名→值)、文本块在 SubImages[].BlockInfo、
  表格在 SubImages[].TableInfo;IsMixedMode 表示该页是否为混贴类型。

本模块只负责「字节 → 归一化文本 + 字段 + 告警」这一原子能力,不含缓存 / 渲染 / 编排
(那三层在 ocr_cache / document_extract)。未配置凭证时 ocr_status() 返回不可用原因,
上层据此优雅降级(跳过 needs_ocr 文件并告警),不阻断整批摄取。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field

from app.config import get_settings

logger = logging.getLogger(__name__)

API_ACTION = "RecognizeAllText"
API_VERSION = "2021-07-07"
FORMAT = "JSON"
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 供应商限制:单请求 10MB

# 内部提取方式 → 阿里云 Type。键与前端「提取方式」一一对应,不做自动切换。
OCR_TYPE_TO_ALIYUN = {
    "general": "Advanced",           # 通用文字识别(高精版)
    "invoice": "Invoice",            # 增值税发票
    "mixed_invoice": "MixedInvoice",  # 混贴票据(一页多张)
    "payment_record": "PaymentRecord",  # 付款详情页
}
OCR_TYPES = tuple(OCR_TYPE_TO_ALIYUN)  # 对外合法的识别类型


def ocr_type_to_aliyun(ocr_type: str) -> str:
    """内部类型 → 供应商实际 Type;未知类型抛 ValueError。"""
    if ocr_type not in OCR_TYPE_TO_ALIYUN:
        raise ValueError(f"未知 OCR 类型 {ocr_type!r}(可选:{', '.join(OCR_TYPES)})")
    return OCR_TYPE_TO_ALIYUN[ocr_type]

# 可重试的业务错误码(临时故障);其余(鉴权 / 参数 / 类型不匹配)一次就抛,不浪费调用。
_RETRYABLE_CODES = frozenset(
    {"Throttling", "Throttling.User", "Throttling.Api", "ServiceUnavailable",
     "InternalError", "ServerError", "Timeout", "RequestTimeout", "ConcurrencyLimit"}
)

_sleep = time.sleep  # 单测里替换掉,避免真等退避时间


class OCRNotConfigured(RuntimeError):
    """未配置 / 未接入可用 OCR 引擎:上层应跳过该 needs_ocr 文件并告警,而非崩。"""


class OCRError(RuntimeError):
    """供应商返回的错误(带错误码,便于上层判断可否重试 / 提示如何配置)。"""

    def __init__(self, code: str, message: str, *, request_id: str | None = None,
                 status: int | None = None, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.request_id = request_id
        self.status = status
        self.retryable = retryable


@dataclass
class OCRPage:
    """一页的识别结果(已归一化)。text 是给切块 / 检索用的正文。"""

    text: str = ""
    fields: dict = field(default_factory=dict)      # 结构化字段(票号 / 金额 …),本次调用内存中归一化用
    recognized_fields: bool = False                # 正文是否来自专用接口的结构化字段(缓存只保留这个标记)
    ocr_type: str = ""                             # 内部类型(general / invoice / …)
    aliyun_type: str = ""                          # 实际调用的供应商 Type
    subimage_count: int = 0
    is_mixed_mode: bool = False                    # 供应商判定「混贴类型」
    warnings: list[str] = field(default_factory=list)
    request_id: str | None = None
    engine: str = "aliyun"
    raw: dict = field(default_factory=dict, repr=False)  # 原始响应,只进缓存、不进向量库


# ---- 签名与请求 ----

def _pct(s: str) -> str:
    """阿里云 RPC 签名的百分号编码:除 A-Za-z0-9-_.~ 外全部转义(空格 → %20,'/' → %2F)。"""
    return urllib.parse.quote(str(s), safe="~")


def _sign_v1(params: dict[str, str], secret: str, method: str = "POST") -> str:
    """RPC 风格 SignatureVersion=1.0(HMAC-SHA1)签名:按参数名排序 → 规范化 → HMAC。"""
    canonical = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(params.items()))
    string_to_sign = f"{method}&{_pct('/')}&{_pct(canonical)}"
    digest = hmac.new((secret + "&").encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(digest.digest()).decode("ascii")


def _build_params(access_key_id: str, aliyun_type: str) -> dict[str, str]:
    """公共参数 + 业务参数(Type)。Timestamp 用 UTC ISO8601,Nonce 用 uuid4。"""
    return {
        "Action": API_ACTION,
        "Version": API_VERSION,
        "Format": FORMAT,
        "AccessKeyId": access_key_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Type": aliyun_type,
    }


def _endpoint() -> str:
    return (get_settings().ocr_aliyun_endpoint or "").strip().removeprefix("https://").removeprefix("http://").rstrip("/")


def endpoint() -> str:
    """识别请求实际使用的端点标识(进缓存键,不含协议、密钥与路径参数)。"""
    return _endpoint()


def _post_once(image: bytes, aliyun_type: str, timeout: float) -> dict:
    """向阿里云发一次请求,成功返回解析后的 JSON;失败抛 OCRError(带可重试标记)。"""
    import httpx  # 惰性 import:未配 OCR 时不拖累启动

    s = get_settings()
    params = _build_params(s.ocr_aliyun_access_key_id, aliyun_type)
    params["Signature"] = _sign_v1(params, s.ocr_aliyun_access_key_secret)
    query = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(params.items()))
    url = f"https://{_endpoint()}/?{query}"

    try:
        resp = httpx.post(
            url,
            content=image,
            headers={"Content-Type": "application/octet-stream"},
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
        )
    except httpx.TimeoutException as exc:
        raise OCRError("NetworkTimeout", f"请求超时({timeout}s):{exc}", retryable=True) from exc
    except httpx.HTTPError as exc:  # 连接 / TLS / 传输层错误:多半是网络抖动,可重试
        raise OCRError("NetworkError", f"网络错误:{exc}", retryable=True) from exc

    try:
        body = resp.json()
    except Exception:
        raise OCRError(
            "BadResponse", f"响应不是 JSON(HTTP {resp.status_code}):{resp.text[:200]}",
            status=resp.status_code, retryable=resp.status_code >= 500,
        ) from None

    code = body.get("Code")
    if code:  # 供应商业务错误:200/4xx 都可能带 Code
        retryable = code in _RETRYABLE_CODES or resp.status_code >= 500
        raise OCRError(
            str(code), str(body.get("Message") or ""), request_id=body.get("RequestId"),
            status=resp.status_code, retryable=retryable,
        )
    if resp.status_code != 200:
        raise OCRError(
            f"HTTP{resp.status_code}", resp.text[:200], status=resp.status_code,
            retryable=resp.status_code >= 500,
        )
    return body


# ---- 响应归一化 ----

def _as_dict(v) -> dict | None:
    """KvInfo.Data 在文档里是字典,但网关可能把它序列化成 JSON 字符串;两种都收。"""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            parsed = json.loads(v)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _kv_lines(kv: dict) -> tuple[list[str], dict]:
    """KvInfo → (["字段: 值", ...], {字段: 值})。Data 解析不出时退回 KvDetails。"""
    lines: list[str] = []
    fields: dict = {}
    data = _as_dict(kv.get("Data"))
    if data:
        for k, v in data.items():
            if v is None or v == "" or v == []:
                continue
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
            lines.append(f"{k}: {v}")
            fields[str(k)] = v
    if lines:
        return lines, fields

    details = kv.get("KvDetails")
    if isinstance(details, dict):  # {字段名: {KeyName, Value, ...}}
        for item in details.values():
            if not isinstance(item, dict):
                continue
            name = item.get("KeyName")
            val = item.get("Value")
            if isinstance(name, str):
                name = name.strip('"')
            if isinstance(val, str):
                val = val.strip('"')
            if not name or val in (None, ""):
                continue
            lines.append(f"{name}: {val}")
            fields[str(name)] = val
    return lines, fields


def _block_lines(sub: dict) -> list[str]:
    blocks = (sub.get("BlockInfo") or {}).get("BlockDetails") or []
    out = []
    for b in blocks:
        t = (b.get("BlockContent") or "").strip()
        if t:
            out.append(t)
    return out


def _table_lines(sub: dict) -> list[str]:
    """表格:按 行 → 单元格 拼成 "单元格 | 单元格"。"""
    tables = (sub.get("TableInfo") or {}).get("TableDetails") or []
    out: list[str] = []
    for t in tables:
        header = t.get("Header") or {}
        for h in header.get("Contents") or []:
            if h:
                out.append(str(h).strip())
        rows: dict[int, list[tuple[int, str]]] = {}
        for cell in t.get("CellDetails") or []:
            content = (cell.get("CellContent") or "").strip()
            if not content:
                continue
            rows.setdefault(int(cell.get("RowStart") or 0), []).append(
                (int(cell.get("ColumnStart") or 0), content)
            )
        for r in sorted(rows):
            out.append(" | ".join(c for _, c in sorted(rows[r])))
    return out


def _subimage_text(sub: dict) -> tuple[list[str], dict]:
    """单个子图 → (正文行, 字段)。优先结构化 KV,其次文本块,再次表格。"""
    lines, fields = _kv_lines(sub.get("KvInfo") or {})
    if not lines:
        lines = _block_lines(sub)
    if not lines:
        lines = _table_lines(sub)
    return lines, fields


def _subimage_parts(subs: list, ticket_like: bool) -> tuple[list[str], dict]:
    """子图列表 → (每张的正文段, 合并后的字段)。多张时给每张加【第 N 张】标签。"""
    parts: list[str] = []
    fields: dict = {}
    for i, sub in enumerate(subs):
        if not isinstance(sub, dict):
            continue
        lines, f = _subimage_text(sub)
        if f:
            prefix = f"{i + 1}." if len(subs) > 1 else ""
            fields.update({f"{prefix}{k}" if prefix else k: v for k, v in f.items()})
        if not lines:
            continue
        block = "\n".join(lines)
        if len(subs) > 1:
            label = (sub.get("Type") or "").strip()
            block = f"【第 {i + 1} 张】{label}\n{block}"
        parts.append(block)
    return parts, fields


def normalize(raw: dict, ocr_type: str) -> OCRPage:
    """供应商响应 → OCRPage(可检索正文 + 结构化字段 + 覆盖告警)。

    正文取法按类型分:通用 / 表格类以整页聚合文本 Content 为准(阅读顺序最完整);
    票据类以 SubImages 的结构化字段为准(字段名: 值的形态最好检索)。两者都空才算失败。
    """
    data = raw.get("Data")
    if isinstance(data, str):  # 少见:Data 被序列化成字符串
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}

    content = (data.get("Content") or "").strip()
    subs = data.get("SubImages") or []
    warnings: list[str] = []

    ticket_like = ocr_type in ("invoice", "mixed_invoice", "payment_record")
    # 票据类优先用结构化字段;通用 / 表格类优先 Content,Content 为空时退回子图文本块。
    use_subs = bool(subs) and (ticket_like or not content)
    parts, fields = _subimage_parts(subs, ticket_like) if use_subs else ([], {})
    body = "\n\n".join(parts).strip()

    if not body:
        body = content
        if content and ticket_like:
            warnings.append("结构化字段为空,已退回整页文本")
    elif content and ticket_like:
        # 结构化字段通常够用;明显偏短时给出覆盖提示,由人工决定是否改用通用识别重跑。
        if len(body) < len(content) * 0.3:
            warnings.append(f"结构化字段可能不完整(字段文本 {len(body)} 字 / 整页文本 {len(content)} 字)")

    if not body:
        warnings.append("未识别出文本(空白页或图片质量过低)")

    sub_count = int(data.get("SubImageCount") or len(subs) or 0)
    if ocr_type == "mixed_invoice" and sub_count == 1 and not data.get("IsMixedMode"):
        warnings.append("按混贴票据提交,但只识别出 1 张票据")

    return OCRPage(
        text=body,
        fields=fields,
        recognized_fields=bool(fields),
        ocr_type=ocr_type,
        aliyun_type=OCR_TYPE_TO_ALIYUN.get(ocr_type, ""),
        subimage_count=sub_count,
        is_mixed_mode=bool(data.get("IsMixedMode")),
        warnings=warnings,
        request_id=raw.get("RequestId"),
        raw=raw,
    )


# ---- 对外入口 ----

def ocr_status() -> tuple[bool, str]:
    """(是否可用, 不可用原因)。可用 = 引擎选了 aliyun 且凭证齐全。"""
    engine = (get_settings().ocr_engine or "").strip().lower()
    if not engine:
        return False, "未配置 OCR_ENGINE(需设为 aliyun);needs_ocr 文件将在摄取时跳过"
    if engine != "aliyun":
        return False, f"未知 OCR_ENGINE={engine!r}(当前仅接入 aliyun)"
    s = get_settings()
    missing = [
        name for name, val in (
            ("OCR_ALIYUN_ACCESS_KEY_ID", s.ocr_aliyun_access_key_id),
            ("OCR_ALIYUN_ACCESS_KEY_SECRET", s.ocr_aliyun_access_key_secret),
            ("OCR_ALIYUN_ENDPOINT", s.ocr_aliyun_endpoint),
        ) if not val
    ]
    if missing:
        return False, "未配置 " + " / ".join(missing) + ":请在 backend/.env 填好阿里云 OCR 凭证"
    return True, ""


def ocr_available() -> bool:
    """是否配了可用的 OCR 引擎。摄取前先问它,避免逐个文件白白抛异常。"""
    return ocr_status()[0]


def recognize_raw(image: bytes, ocr_type: str, *, timeout: float | None = None,
                  retries: int | None = None) -> dict:
    """一张图片字节 → 供应商原始响应(dict)。缓存编排在 ocr_cache,这里不碰存储。

    超时 / 限流 / 5xx 做有限次退避重试;鉴权、参数、类型不匹配等一次性错误立即上抛,
    不反复重试(重试也只会重复计费)。未配置凭证抛 OCRNotConfigured。
    """
    if ocr_type not in OCR_TYPE_TO_ALIYUN:
        raise ValueError(f"未知 OCR 类型 {ocr_type!r}(可选:{', '.join(OCR_TYPES)})")
    ok, reason = ocr_status()
    if not ok:
        raise OCRNotConfigured(reason)
    if not image:
        raise ValueError("图片字节为空")
    if len(image) > MAX_IMAGE_BYTES:
        raise OCRError(
            "ImageTooLarge",
            f"图片 {len(image) / 1024 / 1024:.1f}MB 超过接口上限 {MAX_IMAGE_BYTES // 1024 // 1024}MB",
        )

    s = get_settings()
    timeout = s.ocr_timeout_seconds if timeout is None else timeout
    retries = s.ocr_max_retries if retries is None else retries
    aliyun_type = ocr_type_to_aliyun(ocr_type)

    last: OCRError | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            return _post_once(image, aliyun_type, timeout)
        except OCRError as exc:
            last = exc
            if not exc.retryable or attempt >= max(0, retries):
                logger.warning("OCR 失败(%s)%s", exc.code, "已用尽重试" if exc.retryable else "不可重试")
                raise
            delay = min(4.0, 0.5 * (2 ** attempt))
            logger.warning("OCR 第%d次失败(%s),%.1fs 后重试", attempt + 1, exc.code, delay)
            _sleep(delay)
    raise last or OCRError("Unknown", "OCR 未执行")


def recognize(image: bytes, ocr_type: str, *, timeout: float | None = None,
              retries: int | None = None) -> OCRPage:
    """一张图片字节 → OCRPage(不做缓存;带缓存的入口见 ocr_cache.recognize_cached)。"""
    raw = recognize_raw(image, ocr_type, timeout=timeout, retries=retries)
    page = normalize(raw, ocr_type)
    logger.info(
        "OCR 成功 type=%s 子图=%d 文本=%d字 request_id=%s",
        page.aliyun_type, page.subimage_count, len(page.text), page.request_id,
    )
    return page
