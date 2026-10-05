"""密码策略与哈希:Argon2id,永不截断、永不「顺手修正」。

策略(技术方案 §4,明确写死,前后端提示一致):
- 长度 8–128 个字符(按 **Unicode 码点** 计,不是字节);
- 允许任意字符(含空格、中文、emoji);不做「必须含大小写数字符号」的组合要求
  (NIST 800-63B 取向:长度比组合更重要);
- 先做 Unicode NFC 归一化再哈希 —— 否则同一个密码在不同输入法下可能算出不同哈希;
- 超长直接**拒绝**,绝不静默截断(截断会让「密码后 100 位」变得毫无意义);
- 哈希参数写在 Argon2 编码串里($argon2id$v=19$m=...,t=...,p=...$盐$哈希),
  日后调参时旧哈希仍可验证,登录成功后再按新参数重哈希(needs_rehash)。
"""

from __future__ import annotations

import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from app.auth.errors import AuthNotConfigured, InvalidPassword

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

# 参数固定在模块里(不放进 .env):改这里 = 新哈希用新参数,旧哈希仍能验证。
# 64 MiB / 3 轮 / 4 路是 argon2-cffi 的推荐档,单次约几十毫秒;ECS 8GB 内存足够。
_hasher = PasswordHasher(
    time_cost=3, memory_cost=64 * 1024, parallelism=4,
    hash_len=32, salt_len=16, type=Type.ID,
)


def normalize_password(password: str) -> str:
    """NFC 归一化:同一个「看起来一样」的密码在任何输入法下都得到同一个哈希。"""
    return unicodedata.normalize("NFC", password or "")


def validate_password(password: str | None) -> str | None:
    """校验密码策略;返回错误文案,合法返回 None。"""
    value = normalize_password(password or "")
    if len(value) < MIN_PASSWORD_LENGTH:
        return f"密码至少 {MIN_PASSWORD_LENGTH} 个字符"
    if len(value) > MAX_PASSWORD_LENGTH:
        return f"密码最长 {MAX_PASSWORD_LENGTH} 个字符(不做截断,请自行缩短)"
    return None


def check_password(password: str | None) -> None:
    """校验密码策略,不合格抛 InvalidPassword(400 文案可直接展示)。

    **所有入口都该调这个,而不是 validate_password()** —— 后者返回错误串,
    忘了看返回值就等于放行(建管理员脚本就踩过:弱口令静默通过)。
    """
    message = validate_password(password)
    if message:
        raise InvalidPassword(message)


def hash_password(password: str) -> str:
    """算出 Argon2id 编码串(含盐与参数),直接入库。"""
    return _hasher.hash(normalize_password(password))


def verify_password(password_hash: str, password: str) -> bool:
    """校验密码。任何异常(哈希损坏、参数不支持)都算「不匹配」,不向上抛。"""
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, normalize_password(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:  # noqa: BLE001 —— 哈希层的意外错误同样按「不匹配」处理,不给探测口
        return False


def needs_rehash(password_hash: str) -> bool:
    """哈希参数是否落后于当前配置(登录成功后按需重哈希)。"""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:  # noqa: BLE001
        return False


_dummy_hash: str | None = None


def dummy_verify(raw: str | None = None) -> None:
    """对不存在的账号也做一次同样代价的校验,抹平「有没有这个邮箱」的时序差。

    第一次调用会现算一个哈希(约几十毫秒)并缓存,后续复用。
    """
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("timing-equalizer-not-a-real-password")
    verify_password(_dummy_hash, raw or "timing-equalizer-not-a-real-password")


def self_check() -> None:
    """启动自检:Argon2 后端可用(缺 cffi / 二进制不匹配时在此暴露,而不是等到有人注册)。"""
    if not verify_password(hash_password("self-check"), "self-check"):
        raise AuthNotConfigured("密码哈希自检失败:Argon2id 后端行为异常")
