"""令牌:随机令牌(刷新 / 邮箱)与 access JWT 的签发、校验。

两类令牌,用途不同、绝不混用:
- **随机令牌**(refresh / 邮箱验证 / 重置密码):secrets.token_urlsafe(48) 的高熵随机串,
  只以 SHA-256 摘要入库;原文只出现在响应体 / 邮件链接 / HttpOnly cookie 里。
  服务端不做「先解密再比对」,而是按摘要**查行**,天然恒定时间且不怕库被读。
- **access JWT**:HS256 短时效,claims 固定为 sub / sid / ver / iss / aud / iat / exp(+ typ / jti)。
  算法写死(校验时只接受 HS256,绝不从 token 头里取 alg),密钥必须是够强的高熵配置,
  缺失时抛 AuthNotConfigured —— 没有「弱默认密钥」这种降级。

JWT 只承载身份指针:每次请求仍要回库核对用户状态、会话是否被撤销、auth_version 是否匹配
(见 deps.py)。JWT 本身不是「登录凭证的全部」,撤销因此能立即生效。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_settings
from app.auth.errors import AuthNotConfigured, InvalidToken

ALGORITHM = "HS256"
TOKEN_TYPE = "access"
MIN_SECRET_BYTES = 32          # 256 位;短于此直接拒绝签发(也拒绝启动自检)
# 随机令牌的字节数:token_urlsafe(48) 得到 64 个 URL 安全字符 ≈ 384 位熵,
# 远超「猜不中」的量级;短了就没意义,所以这是个定值而不是可配项。
RANDOM_TOKEN_BYTES = 48

# 明显是「占位 / 示例」的密钥:即便长度够也拒绝,免得有人把文档里的例子抄进 .env。
_WEAK_SECRETS = frozenset({
    "change-me", "changeme", "secret", "password", "qa-agent", "test", "dev",
    "your-secret-key", "please-change-me", "0123456789",
})


def jwt_secret() -> str:
    """取签名密钥;未配置 / 过弱时抛 AuthNotConfigured(绝不发一个谁都能伪造的令牌)。"""
    secret = (get_settings().auth_jwt_secret or "").strip()
    if not secret:
        raise AuthNotConfigured(
            "未配置 AUTH_JWT_SECRET:认证接口不可用。生成方式:"
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    if len(secret.encode("utf-8")) < MIN_SECRET_BYTES or secret.lower() in _WEAK_SECRETS:
        raise AuthNotConfigured(
            "AUTH_JWT_SECRET 太弱:至少 32 字节高熵随机串,且不能是示例值。生成方式:"
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    return secret


def sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_random_token() -> tuple[str, str]:
    """生成一枚随机令牌,返回 (原文, 摘要)。原文只发给用户,库里只存摘要。"""
    raw = secrets.token_urlsafe(RANDOM_TOKEN_BYTES)
    return raw, sha256_hex(raw)


def new_id() -> str:
    """实体 id(UUID4 字符串,36 位,与 migrations 里 CHAR(36) 对齐)。"""
    import uuid

    return str(uuid.uuid4())


def constant_time_equal(a: str, b: str) -> bool:
    """恒定时间比较(CSRF 令牌等需要手动比对的地方用)。"""
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def encode_access_token(
    *,
    user_id: str,
    session_id: str,
    auth_version: int,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """签发 access token,返回 (token, 过期时间)。

    now 传**朴素时间**时按 UTC 解释(与库里的 DATETIME(6) 同一约定)。这一步不能省:
    朴素 datetime 的 .timestamp() 会按本机时区换算,在 UTC+8 的机器上签出来的令牌
    会凭空早 8 小时 —— 一签发就已过期,而且只在非 UTC 时区现形。
    """
    settings = get_settings()
    issued = now or datetime.now(timezone.utc)
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    issued = issued.replace(microsecond=0)
    expires = issued + timedelta(minutes=max(1, int(settings.auth_access_token_minutes)))
    payload = {
        "sub": user_id,
        "sid": session_id,
        "ver": int(auth_version),
        "iss": settings.auth_jwt_issuer,
        "aud": settings.auth_jwt_audience,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "typ": TOKEN_TYPE,
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, jwt_secret(), algorithm=ALGORITHM), expires


def decode_access_token(token: str) -> dict:
    """校验 access token(签名 / 算法 / iss / aud / exp / 必备 claims),返回 payload。

    任何问题都抛 InvalidToken(调用方统一 401),不把 PyJWT 的细节透给客户端。

    时间一律用真实时钟:PyJWT 3 起不再接受 now= 这类额外参数(2.x 会静默忽略并告警),
    「过期令牌」的用例请用 encode_access_token(now=过去时刻) 造令牌,而不是指望这里能改时间。
    """
    if not token:
        raise InvalidToken("缺少访问令牌")
    try:
        payload = jwt.decode(
            token,
            jwt_secret(),
            algorithms=[ALGORITHM],          # 写死算法:绝不接受 token 头里声明的 alg
            issuer=get_settings().auth_jwt_issuer,
            audience=get_settings().auth_jwt_audience,
            options={"require": ["exp", "iat", "sub", "sid", "ver", "iss", "aud"]},
            leeway=5,                        # 容忍几秒时钟漂移
        )
    except AuthNotConfigured:
        raise
    except jwt.PyJWTError as exc:
        raise InvalidToken(f"访问令牌无效:{type(exc).__name__}") from exc

    if payload.get("typ") != TOKEN_TYPE:
        raise InvalidToken("令牌类型不对")
    for key in ("sub", "sid"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise InvalidToken(f"访问令牌缺少 {key}")
    if not isinstance(payload.get("ver"), int):
        raise InvalidToken("访问令牌缺少 ver")
    return payload


def bearer_token(authorization: str | None) -> str:
    """从 Authorization 头里取出 Bearer 令牌;拿不到返回空串。"""
    header = (authorization or "").strip()
    if not header:
        return ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()
