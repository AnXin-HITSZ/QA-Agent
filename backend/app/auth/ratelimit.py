"""认证接口限流:Redis 固定窗口计数,多 worker 共享同一份计数。

为什么必须有:登录、注册、找回密码、刷新令牌这些接口是暴力破解与撞库的入口,
单靠密码强度不够(见技术方案 §5)。计数放 Redis 而不是进程内存 —— uvicorn 多 worker /
多机部署时,进程内计数会各算各的,等于没限。

**失败关闭**:Redis 未配置或连不上时抛 RateLimitUnavailable,接口返回 503,
不放开限制 —— 否则「刚好 Redis 抖一下」就是一次不限速的暴力破解窗口。

测试用 MemoryLimiter(进程内),绝不连真 Redis。
"""

from __future__ import annotations

import logging
import time

from app.auth.errors import RateLimited, RateLimitUnavailable
from app.config import get_settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "qa:auth:rl:"

# 窗口长度按用途区分:越敏感的接口窗口越短、额度越小(见各路由的调用点)。
WINDOW_SECONDS = 300


class Limiter:
    async def hit(self, key: str, *, limit: int, window_seconds: int = WINDOW_SECONDS) -> None:
        raise NotImplementedError


class RedisLimiter(Limiter):
    """固定窗口:INCR 计数 + 首次出现时设 EXPIRE。计数不落业务库,Redis 丢键 = 重新计数。"""

    def __init__(self, client) -> None:
        self._client = client

    async def hit(self, key: str, *, limit: int, window_seconds: int = WINDOW_SECONDS) -> None:
        full = KEY_PREFIX + key
        try:
            count = await self._client.incr(full)
            if int(count) == 1:
                await self._client.expire(full, max(1, int(window_seconds)))
            if int(count) > limit:
                ttl = await self._client.ttl(full)
                raise RateLimited(retry_after=max(1, int(ttl if ttl and ttl > 0 else window_seconds)))
        except RateLimited:
            raise
        except Exception as exc:  # noqa: BLE001 —— 连接 / 超时 / 命令错误都按「限流设施不可用」
            raise RateLimitUnavailable(f"限流设施不可用:{type(exc).__name__}: {exc}") from exc


class MemoryLimiter(Limiter):
    """仅测试:进程内固定窗口,语义与 RedisLimiter 一致(含失败关闭之外的正常路径)。"""

    def __init__(self) -> None:
        self.hits: dict[str, tuple[int, float]] = {}   # key -> (计数, 窗口到期时间)

    async def hit(self, key: str, *, limit: int, window_seconds: int = WINDOW_SECONDS) -> None:
        now = time.monotonic()
        count, expires = self.hits.get(key, (0, 0.0))
        if expires <= now:
            count, expires = 0, now + max(1, int(window_seconds))
        count += 1
        self.hits[key] = (count, expires)
        if count > limit:
            raise RateLimited(retry_after=max(1, int(expires - now) + 1))


_limiter: Limiter | None = None
_client = None


def install(limiter: Limiter | None) -> None:
    """安装限流实现(测试用);传 None 恢复按配置懒建 Redis 实现。"""
    global _limiter
    _limiter = limiter


async def _redis_limiter() -> Limiter:
    global _client, _limiter
    if _limiter is not None:
        return _limiter
    url = (get_settings().redis_url or "").strip()
    if not url:
        raise RateLimitUnavailable(
            "未配置 Redis(REDIS_URL):认证接口的限流依赖它,当前拒绝服务(失败关闭)。"
        )
    try:
        from redis.asyncio import Redis

        _client = Redis.from_url(          # 连接加固与 checkpointer / 待办一致
            url, decode_responses=True, socket_keepalive=True, health_check_interval=30,
            socket_connect_timeout=float(get_settings().cache_redis_timeout_seconds),
            socket_timeout=float(get_settings().cache_redis_timeout_seconds),
        )
        _limiter = RedisLimiter(_client)
    except Exception as exc:  # noqa: BLE001
        raise RateLimitUnavailable(f"限流设施不可用:{type(exc).__name__}: {exc}") from exc
    return _limiter


async def hit(key: str, *, limit: int, window_seconds: int = WINDOW_SECONDS) -> None:
    """记一次访问;超限抛 RateLimited,设施不可用抛 RateLimitUnavailable。

    key 由调用方拼(如 "login:ip:1.2.3.4" / "login:email:a@b.c");内存里绝不拼完整邮箱,
    见 app.auth.emails.mask_email —— 这里用摘要更稳妥。
    """
    if not get_settings().auth_rate_limit_enabled:
        return
    limiter = await _redis_limiter()
    await limiter.hit(key, limit=limit, window_seconds=window_seconds)


async def close() -> None:
    """关停时释放连接(应用 lifespan 调用)。"""
    global _client, _limiter
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("关闭限流 Redis 连接失败(忽略)", exc_info=True)
    _client = None
    _limiter = None
