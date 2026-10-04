"""调用日志脱敏:只留排查所需的最少信息,绝不落密钥 / 签名 / 完整 URL / 原文。

调用日志要长期保存(MySQL),所以每一条都要先过这里:
- 错误信息截断到固定长度并抹掉疑似凭据(签名、AccessKey、Bearer、sk- 前缀等);
- endpoint 只留主机名(去掉协议、路径与全部查询参数 —— OCR 的签名就在查询串里);
- 不提供任何「记录请求体 / 响应体 / 原文」的入口:调用方拿不到这样的函数。

这是防泄漏的最后一道闸:即便上层不小心把整个异常字符串塞进来,也不会原样入库。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

MAX_ERROR_MESSAGE = 300
MAX_ENDPOINT = 255

# 先抹凭据形态,再截断 —— 顺序不能反,否则被截断的密钥片段仍可能漏出。
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{4,}"), r"\1 ***"),
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{6,}"), "sk-***"),
    (re.compile(r"\bLTAI[A-Za-z0-9]{6,}"), "LTAI***"),
    (
        re.compile(
            r"(?i)\b(signature|signaturenonce|accesskeyid|access_key_id|access_key_secret"
            r"|api[_-]?key|apikey|token|password|passwd|secret)\b\s*[=:]\s*[^\s&,;'\"<>]+"
        ),
        r"\1=***",
    ),
    (re.compile(r"(?i)\b(https?://[^\s\"'<>]*?)\?[^\s\"'<>]*"), r"\1?***"),
)
_WS = re.compile(r"\s+")


def safe_text(value: object, *, limit: int = MAX_ERROR_MESSAGE) -> str:
    """任意对象 → 抹掉凭据形态、压平空白、按上限截断的可入库字符串。"""
    text = "" if value is None else str(value)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    text = _WS.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def safe_endpoint(value: object) -> str:
    """端点标识 → 主机名[:端口];解析不出就返回去凭据后的短串(绝不含查询串)。"""
    raw = "" if value is None else str(value).strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return safe_text(value, limit=MAX_ENDPOINT)
    host = parts.hostname or ""
    if not host:
        return safe_text(value, limit=MAX_ENDPOINT)
    port = f":{parts.port}" if parts.port else ""
    return (host + port)[:MAX_ENDPOINT]


def safe_error(exc: BaseException) -> tuple[str, str]:
    """异常 → (类名, 脱敏后的消息)。类名本身不含敏感信息,原样保留。"""
    return type(exc).__name__, safe_text(exc)
