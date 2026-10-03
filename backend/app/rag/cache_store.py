"""缓存后端:抽象读写接口 + Redis(String/JSON)实现 + 内存实现(仅测试)。

三层结果缓存(OCR 识别 / 文本转换 / embedding)、文件登记与清单都存 Redis String;
结果与清单无 TTL,计算锁有 TTL 并带持有者令牌 —— 见
docs/Redis缓存与文件身份管理技术方案.md §4 / §5 / §9。

两条铁律(方案 §5 / §9):
- 查询成功但 key 不存在才是「未命中」;连接失败、权限错误、读取失败一律抛
  CacheUnavailable,绝不当作未命中继续付费调用;
- 结果一律整值原子写入(SET 覆盖),禁止多步写入半成品。

后端由 CACHE_BACKEND 选择:
  redis  默认:用 REDIS_URL 连共享 Redis(与 checkpointer / 待办同一实例,`qa:` 前缀隔离)。
         应用只做读写,不修改共享 Redis 的 maxmemory / 淘汰策略等全局配置(§9)。
  memory 仅测试:进程内字典,提供同样的令牌锁语义,由测试夹具安装以隔离。
  none   显式关闭:每次都重新计算(会重复付费调用),只留给本地无 Redis 的开发环境。

删除一律按「清单里列出的具体键」执行,不做 FLUSHDB,也不按前缀模糊删除(§8)。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Iterator, Protocol, TypeVar

from app.config import get_settings

logger = logging.getLogger(__name__)

KEY_ROOT = "qa:"
CACHE_PREFIX = KEY_ROOT + "cache:"        # qa:cache:<层>:<版本>:<digest>
LOCK_PREFIX = KEY_ROOT + "lock:"          # qa:lock:<层>:<digest>
DOCUMENT_PREFIX = KEY_ROOT + "document:"  # qa:document:<document_id>:record | :manifest
PATH_PREFIX = KEY_ROOT + "path:"          # qa:path:<路径标识摘要>

# Redis 达到 maxmemory + noeviction 时给用户看的文案(§9;经任务错误通道传到前端)。
OOM_MESSAGE = (
    "Redis 已达到配置的内存上限,无法保存新的缓存结果,本次索引已暂停。"
    "请释放空间或提高内存上限后重试。已有索引仍可使用。"
)
NOT_CONFIGURED_MESSAGE = (
    "缓存未接通:请配置 REDIS_URL(见 docs/Redis缓存与文件身份管理技术方案.md §9);"
    "本地无 Redis 时可显式设置 CACHE_BACKEND=none,但那会重复付费调用,仅限开发环境。"
)
BUSY_MESSAGE = (
    "同一内容正在其它进程 / 任务中计算,本次没有抢到计算锁。"
    "为避免重复付费调用,不会绕过锁自行计算;请稍后重试(通常等对方算完即可)。"
)


# ---- 错误分类 ----

class CacheError(RuntimeError):
    """缓存层错误基类。继承 RuntimeError:接口层 _dep_503 自动转 503。"""


class CacheNotConfigured(CacheError):
    """没配 REDIS_URL 或 CACHE_BACKEND 取值不合法:配置错误,不是未命中。"""


class CacheUnavailable(CacheError):
    """连接失败 / 权限错误 / 读取失败:绝不当成未命中,也不再继续付费调用。"""


class CacheWriteFailed(CacheError):
    """写入失败(非内存上限)。"""


class CacheOutOfMemory(CacheWriteFailed):
    """Redis maxmemory + noeviction 拒绝写入:暂停任务并给出明确报错(§9)。"""

    def __init__(self, detail: str = "") -> None:
        super().__init__(OOM_MESSAGE + (f"(Redis:{detail})" if detail else ""))
        self.detail = detail


class CacheBusy(CacheError):
    """等锁超时:同一内容正在别处计算 —— 可重试的忙碌状态,不是失败也不是未命中。

    绝不在没有锁的情况下自行计算(§5):宁可让调用方稍后重试,也不重复付费。
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(BUSY_MESSAGE + (f"(正在计算:{detail})" if detail else ""))
        self.detail = detail


# ---- 抽象接口 ----

class CacheBackend(Protocol):
    """缓存读写接口(同步):三层缓存、登记与清单都只用这几个原语。"""

    enabled: bool

    def check(self) -> None:
        """可用性自检;不可用抛 CacheError。"""

    def get(self, key: str) -> str | None:
        """读一个 String;不存在返回 None,读取失败抛 CacheUnavailable。"""

    def mget(self, keys: list[str]) -> list[str | None]: ...

    def set(self, key: str, value: str) -> None:
        """整值原子写;失败抛 CacheWriteFailed(内存上限抛 CacheOutOfMemory)。"""

    def set_if_absent(self, key: str, value: str) -> bool:
        """SET NX:返回是否由本次写入(路径首次登记靠它原子完成,§6)。"""

    def delete(self, keys: list[str]) -> int:
        """按明确列出的键删除,返回实际删除个数。"""

    def acquire_lock(self, key: str, ttl: float) -> str | None:
        """原子取锁并返回随机持有者令牌;没拿到返回 None。"""

    def renew_lock(self, key: str, token: str, ttl: float) -> bool:
        """续期(校验令牌);锁已易主返回 False。"""

    def release_lock(self, key: str, token: str) -> bool:
        """释放(校验令牌);不是自己的锁绝不删除。"""


# ---- Redis 实现 ----

_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
    "else return 0 end"
)
_RENEW_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) "
    "else return 0 end"
)


class RedisCache:
    """Redis String/JSON 实现:结果缓存无 TTL,锁用 SET NX PX + Lua 校验令牌。"""

    enabled = True

    def __init__(self, url: str, *, timeout: float = 5.0) -> None:
        self._url = url
        self._timeout = timeout
        self._client = None

    def _c(self):
        if self._client is None:
            import redis

            try:
                self._client = redis.Redis.from_url(
                    self._url,
                    decode_responses=True,
                    socket_timeout=self._timeout,
                    socket_connect_timeout=self._timeout,
                )
            except ValueError as exc:  # URL 形态不对(缺 scheme 等)
                raise CacheNotConfigured(f"REDIS_URL 不合法:{exc}") from exc
        return self._client

    def _translate(self, exc: Exception, *, writing: bool) -> CacheError:
        import redis.exceptions as rexc

        if isinstance(exc, rexc.ResponseError):
            msg = str(exc)
            if "OOM" in msg or "maxmemory" in msg:
                return CacheOutOfMemory(msg)
            return (
                CacheWriteFailed(f"Redis 写入失败:{msg}") if writing
                else CacheUnavailable(f"Redis 读取失败:{msg}")
            )
        if isinstance(exc, (rexc.ConnectionError, rexc.TimeoutError)):
            return CacheUnavailable(f"Redis 连接失败:{exc}")
        if isinstance(exc, rexc.AuthenticationError):
            return CacheUnavailable(f"Redis 认证失败:请检查 REDIS_URL 里的密码({exc})")
        if isinstance(exc, (rexc.NoPermissionError, rexc.RedisError)):
            return CacheUnavailable(f"Redis 调用失败:{exc}")
        return CacheUnavailable(f"Redis 调用异常:{type(exc).__name__}:{exc}")

    def _call(self, fn: Callable, *args, writing: bool = False, **kwargs):
        try:
            return fn(*args, **kwargs)
        except CacheError:
            raise
        except Exception as exc:
            raise self._translate(exc, writing=writing) from exc

    def check(self) -> None:
        self._call(self._c().ping)

    def get(self, key: str) -> str | None:
        return self._call(self._c().get, key)

    def mget(self, keys: list[str]) -> list[str | None]:
        if not keys:
            return []
        return list(self._call(self._c().mget, keys))

    def set(self, key: str, value: str) -> None:
        self._call(self._c().set, key, value, writing=True)

    def set_if_absent(self, key: str, value: str) -> bool:
        return bool(self._call(self._c().set, key, value, nx=True, writing=True))

    def delete(self, keys: list[str]) -> int:
        keys = [k for k in keys if k]
        if not keys:
            return 0
        return int(self._call(self._c().delete, *keys, writing=True))

    def acquire_lock(self, key: str, ttl: float) -> str | None:
        token = uuid.uuid4().hex
        got = self._call(self._c().set, key, token, nx=True, px=max(1, int(ttl * 1000)), writing=True)
        return token if got else None

    def renew_lock(self, key: str, token: str, ttl: float) -> bool:
        got = self._call(self._c().eval, _RENEW_LUA, 1, key, token, max(1, int(ttl * 1000)),
                         writing=True)
        return bool(got)

    def release_lock(self, key: str, token: str) -> bool:
        got = self._call(self._c().eval, _RELEASE_LUA, 1, key, token, writing=True)
        return bool(got)


# ---- 内存实现(仅测试) ----

class MemoryCache:
    """进程内字典实现:语义与 Redis 实现一致(含令牌锁与 TTL),供测试隔离安装。"""

    enabled = True

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._locks: dict[str, tuple[str, float]] = {}
        self._mutex = threading.RLock()
        self.writes = 0

    def check(self) -> None:
        return None

    def get(self, key: str) -> str | None:
        with self._mutex:
            return self._data.get(key)

    def mget(self, keys: list[str]) -> list[str | None]:
        with self._mutex:
            return [self._data.get(k) for k in keys]

    def set(self, key: str, value: str) -> None:
        with self._mutex:
            self._data[key] = value
            self.writes += 1

    def set_if_absent(self, key: str, value: str) -> bool:
        with self._mutex:
            if key in self._data:
                return False
            self._data[key] = value
            self.writes += 1
            return True

    def delete(self, keys: list[str]) -> int:
        n = 0
        with self._mutex:
            for k in keys:
                if k and self._data.pop(k, None) is not None:
                    n += 1
        return n

    def _live(self, key: str) -> str | None:
        got = self._locks.get(key)
        if not got:
            return None
        token, expires = got
        if expires <= time.monotonic():
            self._locks.pop(key, None)
            return None
        return token

    def acquire_lock(self, key: str, ttl: float) -> str | None:
        with self._mutex:
            if self._live(key) is not None:
                return None
            token = uuid.uuid4().hex
            self._locks[key] = (token, time.monotonic() + ttl)
            return token

    def renew_lock(self, key: str, token: str, ttl: float) -> bool:
        with self._mutex:
            if self._live(key) != token:
                return False
            self._locks[key] = (token, time.monotonic() + ttl)
            return True

    def release_lock(self, key: str, token: str) -> bool:
        with self._mutex:
            if self._live(key) != token:
                return False
            self._locks.pop(key, None)
            return True


class NullCache:
    """显式关闭缓存(CACHE_BACKEND=none):读永远未命中、写丢弃、锁形同虚设。

    只留给本地无 Redis 的开发环境 —— 每次都会重新调用付费接口。
    """

    enabled = False

    def check(self) -> None:
        return None

    def get(self, key: str) -> str | None:
        return None

    def mget(self, keys: list[str]) -> list[str | None]:
        return [None] * len(keys)

    def set(self, key: str, value: str) -> None:
        return None

    def set_if_absent(self, key: str, value: str) -> bool:
        return True

    def delete(self, keys: list[str]) -> int:
        return 0

    def acquire_lock(self, key: str, ttl: float) -> str | None:
        return uuid.uuid4().hex

    def renew_lock(self, key: str, token: str, ttl: float) -> bool:
        return True

    def release_lock(self, key: str, token: str) -> bool:
        return True


# ---- 后端单例 ----

_backend: CacheBackend | None = None
_backend_sig: tuple | None = None
_installed = False       # True = 测试显式安装的后端,不再按配置重建


def _build(name: str) -> CacheBackend:
    s = get_settings()
    if name == "none":
        logger.warning("缓存已显式关闭(CACHE_BACKEND=none):OCR / 向量化会重复付费调用,仅限开发环境")
        return NullCache()
    if name == "memory":
        return MemoryCache()
    if name == "redis":
        if not s.redis_url:
            raise CacheNotConfigured(NOT_CONFIGURED_MESSAGE)
        return RedisCache(s.redis_url, timeout=s.cache_redis_timeout_seconds)
    raise CacheNotConfigured(f"未知 CACHE_BACKEND={name!r}(可选:redis / memory / none)")


def get_cache() -> CacheBackend:
    """当前缓存后端。配置变化(测试替换 .env)时自动重建;显式安装的后端优先。"""
    global _backend, _backend_sig
    if _installed:
        return _backend                     # type: ignore[return-value]  install() 保证非空
    s = get_settings()
    sig = ((s.cache_backend or "redis").strip().lower(), s.redis_url, s.cache_redis_timeout_seconds)
    if _backend is None or _backend_sig != sig:
        _backend = _build(sig[0])
        _backend_sig = sig
    return _backend


def install(backend: CacheBackend | None) -> None:
    """显式安装后端(None = 恢复按配置构造)。测试夹具用,业务代码不要调用。"""
    global _backend, _backend_sig, _installed
    _installed = backend is not None
    _backend = backend if backend is not None else _backend
    if backend is None:
        _backend_sig = None                 # 逼 get_cache() 按当前配置重建


def ensure_available() -> None:
    """起任务前的缓存自检:未配置 / 连不上都立刻报错,别等跑到一半才失败。"""
    cache = get_cache()
    if not cache.enabled:
        return
    cache.check()


# ---- 计算锁(§5):随机令牌 + TTL + 续期 + 校验后释放 ----

def lock_name(kind: str, identity: str) -> str:
    return f"{LOCK_PREFIX}{kind}:{identity}"


class LockRenewer:
    """后台续期:按 TTL 的一半续一次,可同时续多把锁(一次批量向量化常持多把)。

    公开给 embedding 批量路径使用(单把锁的常规路径由 held_lock 内部使用)。

    续期失败(锁已易主 / Redis 抖动)只告警不打断计算 —— 本次结果照常返回,只是锁到期后
    他人可能重复计算,与进程崩溃时一样,不承诺严格只执行一次(§5)。
    """

    def __init__(self, backend: CacheBackend, locks: list[tuple[str, str]], ttl: float,
                 *, interval: float | None = None) -> None:
        self._backend, self._locks, self._ttl = backend, locks, ttl
        self._interval = max(1.0, ttl / 2) if interval is None else interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="cache-lock-renew", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(max(0.01, self._interval)):
            for key, token in self._locks:
                try:
                    if not self._backend.renew_lock(key, token, self._ttl):
                        logger.warning("计算锁 %s 已易主,停止续期(计算继续,结果可能被重复计算)", key)
                        return
                except CacheError as exc:
                    logger.warning("计算锁续期失败(继续计算,锁到期后他人可接管):%s", exc)
                    return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


@contextmanager
def held_lock(kind: str, identity: str, *, ttl: float | None = None,
              wait: float | None = None) -> Iterator[bool]:
    """计算锁上下文:yield True = 本次持有锁(调用方应计算并写缓存),False = 没拿到。

    没拿到时最多等 wait 秒(轮询重取,等对方算完);超时仍 yield False,由调用方决定
    怎么办 —— single_flight 的选择是再查缓存、仍没有就抛 CacheBusy,**不绕过锁计算**。
    锁过期、进程崩溃、网络超时仍可能导致重复调用,不承诺收费接口严格只执行一次(§5)。
    """
    s = get_settings()
    ttl = float(s.cache_lock_ttl_seconds if ttl is None else ttl)
    wait = float(s.cache_lock_wait_seconds if wait is None else wait)
    cache = get_cache()
    lkey = lock_name(kind, identity)
    token = cache.acquire_lock(lkey, ttl)
    if token is None and wait > 0:
        deadline = time.monotonic() + wait
        while token is None and time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.05, wait / 10)))
            token = cache.acquire_lock(lkey, ttl)
    if token is None:
        yield False
        return
    renewer = LockRenewer(cache, [(lkey, token)], ttl)
    renewer.start()
    try:
        yield True
    finally:
        renewer.stop()
        try:
            cache.release_lock(lkey, token)
        except CacheError as exc:   # 释放失败只能等 TTL,别让清理错误盖住业务结果
            logger.warning("释放计算锁 %s 失败(等 TTL 自动过期):%s", lkey, exc)


T = TypeVar("T")


def single_flight(*, kind: str, identity: str, read: Callable[[], T | None],
                  compute: Callable[[], T], write: Callable[[T], None],
                  keep: Callable[[T], bool] | None = None,
                  ttl: float | None = None, wait: float | None = None) -> tuple[T, bool]:
    """单飞 + 双检的缓存读取模板:返回 (值, 是否命中缓存)。

    命中(read 返回非 None)→ 直接返回;未命中 → 取锁 → 持锁时再查一次(read 双检),
    仍未命中才算,算完经 keep 校验后写缓存。等锁超时则最后再查一次,仍没有就抛
    CacheBusy(可重试的忙碌状态)—— **绝不在没有锁时自行计算**,否则重复付费(§5)。
    """
    hit = read()
    if hit is not None:
        return hit, True

    with held_lock(kind, identity, ttl=ttl, wait=wait) as got:
        if got:
            again = read()          # 双检:等锁期间别人可能已经算好
            if again is not None:
                return again, True
            value = compute()
            if keep is None or keep(value):
                write(value)
            return value, False

    again = read()                  # 等锁超时:最后再查一次(别人可能刚写好还没释放锁)
    if again is not None:
        return again, True
    raise CacheBusy(f"{kind}:{identity[:12]}")


def dumps(payload) -> str:
    """确定性 JSON(缓存值与清单统一用它;键摘要另用 _digest)。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_json(value: str | None, *, what: str = "缓存值"):
    """解析缓存里的 JSON;损坏返回 None(= 按未命中处理)并告警,不抛。"""
    if value is None:
        return None
    try:
        return json.loads(value)
    except Exception as exc:
        logger.warning("%s 解析失败(按未命中处理):%s", what, exc)
        return None


def write_with_retry(cache: CacheBackend, key: str, value: str, *, attempts: int = 3) -> None:
    """写缓存并在失败时短暂重试(进程存活期间优先保存已算出的结果,不重调付费接口,§9)。"""
    last: CacheError | None = None
    for i in range(max(1, attempts)):
        try:
            cache.set(key, value)
            return
        except CacheOutOfMemory:
            raise                     # 内存上限:立刻报错,重试没有意义
        except CacheWriteFailed as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(0.2 * (2 ** i))
    assert last is not None
    raise last
