"""缓存后端:命中 / 未命中 / 失败三分,计算锁令牌,单飞模板,内存上限映射。

不连真实 Redis:错误翻译用假客户端 + 真实 redis 异常类验证(不需要服务器),
其余语义用进程内 MemoryCache。核心断言是方案 §5 / §9 的两条铁律:
「查询成功但 key 不存在」才算未命中;连接失败 / 内存上限绝不当成未命中继续付费调用。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.rag import cache_store
from app.rag.cache_store import (
    CacheBusy,
    CacheNotConfigured,
    CacheOutOfMemory,
    CacheUnavailable,
    CacheWriteFailed,
    LockRenewer,
    MemoryCache,
    NullCache,
    RedisCache,
    held_lock,
    single_flight,
    write_with_retry,
)


@pytest.fixture(autouse=True)
def _reset_backend():
    """每个用例后卸载安装的后端,避免测试间互相影响。"""
    yield
    cache_store.install(None)


# ---- 内存后端语义 ----

def test_memory_cache_set_get_delete():
    c = MemoryCache()
    assert c.get("k") is None                 # 不存在 = 未命中
    c.set("k", "v")
    assert c.get("k") == "v"
    assert c.mget(["k", "nope"]) == ["v", None]
    assert c.delete(["k"]) == 1
    assert c.get("k") is None


def test_memory_lock_token_must_match():
    """续期 / 释放都校验持有者令牌:不是自己的锁绝不删除(§5)。"""
    c = MemoryCache()
    token = c.acquire_lock("lock", ttl=10)
    assert token and c.acquire_lock("lock", ttl=10) is None      # 别人抢不到
    assert c.renew_lock("lock", "别人的令牌", 10) is False
    assert c.release_lock("lock", "别人的令牌") is False
    assert c.get("lock") is None and c._live("lock") == token     # 锁仍在
    assert c.renew_lock("lock", token, 10) is True
    assert c.release_lock("lock", token) is True
    assert c.acquire_lock("lock", ttl=10) is not None             # 释放后可再取


def test_memory_lock_expires_with_ttl():
    c = MemoryCache()
    assert c.acquire_lock("lock", ttl=0.05) is not None
    time.sleep(0.08)
    assert c.acquire_lock("lock", ttl=10) is not None             # 过期后他人可接管


def test_null_cache_is_explicitly_disabled():
    """CACHE_BACKEND=none:读永远未命中(会重复付费,方案已注明仅限开发)。"""
    c = NullCache()
    assert c.enabled is False
    assert c.get("k") is None and c.mget(["k"]) == [None]
    c.set("k", "v")
    assert c.get("k") is None
    assert c.acquire_lock("l", 10) is not None                    # 锁形同虚设


# ---- 配置错误 / 失败 ≠ 未命中 ----

def test_unknown_backend_fails_loudly(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "cache_backend", "bogus")
    with pytest.raises(CacheNotConfigured):
        cache_store.get_cache()


def test_redis_without_url_fails_loudly(monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "cache_backend", "redis")
    monkeypatch.setattr(s, "redis_url", "")
    with pytest.raises(CacheNotConfigured):
        cache_store.get_cache()


class _StubClient:
    """假 redis 客户端:按给定异常失败,不建立任何连接。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def _boom(self, *a, **k):
        self.calls += 1
        raise self.exc

    get = mget = set = delete = eval = ping = _boom


def _stub_cache(exc: Exception) -> tuple[RedisCache, _StubClient]:
    cache = RedisCache("redis://127.0.0.1:6379/0")
    client = _StubClient(exc)
    cache._client = client
    return cache, client


def test_redis_oom_maps_to_dedicated_error_with_pause_message():
    """maxmemory + noeviction 拒绝写入:专用错误 + 给用户看的中文文案(§9)。"""
    import redis.exceptions as rexc

    cache, _ = _stub_cache(rexc.ResponseError(
        "OOM command not allowed when used memory > 'maxmemory'."))
    with pytest.raises(CacheOutOfMemory) as err:
        cache.set("k", "v")
    assert "内存上限" in str(err.value) and "索引已暂停" in str(err.value)
    assert isinstance(err.value, CacheWriteFailed)


def test_redis_connection_failure_is_unavailable_not_miss():
    """连不上:抛 CacheUnavailable —— 绝不能返回 None 让上层当成未命中去付费调用。"""
    import redis.exceptions as rexc

    cache, _ = _stub_cache(rexc.ConnectionError("connection refused"))
    with pytest.raises(CacheUnavailable):
        cache.get("k")
    with pytest.raises(CacheUnavailable):
        cache.mget(["k"])


def test_redis_timeout_is_unavailable():
    import redis.exceptions as rexc

    cache, _ = _stub_cache(rexc.TimeoutError("timed out"))
    with pytest.raises(CacheUnavailable):
        cache.check()


def test_redis_lock_uses_token_and_returns_none_when_taken():
    cache = RedisCache("redis://127.0.0.1:6379/0")

    class _LockClient:
        def __init__(self) -> None:
            self.acquired = True
            self.token = None

        def set(self, key, value, nx=False, px=None):
            assert nx and px and px > 0      # 锁必须带 TTL(SET NX PX)
            if self.acquired:
                self.token = value
            return self.acquired

        def eval(self, script, numkeys, key, token, *args):
            return 1 if token == self.token else 0   # 令牌不符 = 不是自己的锁

    cache._client = _LockClient()
    token = cache.acquire_lock("lock", ttl=1.5)
    assert token                                        # 拿到了,令牌是随机值
    cache._client.acquired = False
    assert cache.acquire_lock("lock", ttl=1.5) is None   # SET NX 失败 = 没抢到
    assert cache.renew_lock("lock", token, 1.5) is True
    assert cache.release_lock("lock", "别人的") is False


# ---- 计算锁(§5)----

def test_held_lock_reports_contention(cache_env):
    with held_lock("ocr", "same", ttl=5, wait=0.1) as first:
        assert first is True
        with held_lock("ocr", "same", ttl=5, wait=0.1) as second:
            assert second is False          # 没抢到:由调用方决定怎么办
    with held_lock("ocr", "same", ttl=5, wait=0.1) as again:
        assert again is True                # 前一个退出时已释放


def test_held_lock_releases_on_exception(cache_env):
    with pytest.raises(ValueError):
        with held_lock("ocr", "boom", ttl=5, wait=0.1):
            raise ValueError("计算失败")
    with held_lock("ocr", "boom", ttl=5, wait=0.1) as got:
        assert got is True                  # 异常路径也释放了令牌


# ---- 单飞模板 ----

def test_single_flight_hit_skips_compute(cache_env):
    cache_env.set("k", "旧值")
    computed: list[int] = []
    value, hit = single_flight(
        kind="t", identity="i",
        read=lambda: cache_env.get("k"),
        compute=lambda: (computed.append(1), "新值")[1],
        write=lambda v: cache_env.set("k", v),
    )
    assert (value, hit) == ("旧值", True)
    assert computed == []


def test_single_flight_miss_computes_and_writes(cache_env):
    value, hit = single_flight(
        kind="t", identity="i",
        read=lambda: cache_env.get("k"),
        compute=lambda: "算出来的",
        write=lambda v: cache_env.set("k", v),
    )
    assert (value, hit) == ("算出来的", False)
    assert cache_env.get("k") == "算出来的"


def test_single_flight_keep_false_does_not_write(cache_env):
    """空结果 / 非法结果:值照常返回,但不写成功缓存(§4.1 / §11)。"""
    value, hit = single_flight(
        kind="t", identity="i",
        read=lambda: cache_env.get("k"),
        compute=lambda: "   ",
        write=lambda v: cache_env.set("k", v),
        keep=lambda v: bool(v.strip()),
    )
    assert value == "   " and hit is False
    assert cache_env.get("k") is None


def test_single_flight_failure_keeps_old_value(cache_env):
    """强制刷新失败:异常上抛,旧缓存原样保留(下次仍可命中)。"""
    cache_env.set("k", "旧值")

    def _boom():
        raise RuntimeError("供应商失败")

    with pytest.raises(RuntimeError):
        single_flight(
            kind="t", identity="i",
            read=lambda: None,              # 刷新:跳过读
            compute=_boom,
            write=lambda v: cache_env.set("k", v),
        )
    assert cache_env.get("k") == "旧值"


def test_single_flight_busy_after_lock_timeout_does_not_compute(cache_env):
    """等锁超时:再查一次仍没有就抛 CacheBusy —— 绝不绕过锁付费计算(§5)。"""
    holder = cache_env.acquire_lock(cache_store.lock_name("t", "i"), ttl=30)   # 别人持有
    computed: list[int] = []
    with pytest.raises(CacheBusy):
        single_flight(
            kind="t", identity="i",
            read=lambda: cache_env.get("k"),
            compute=lambda: (computed.append(1), "兜底结果")[1],
            write=lambda v: cache_env.set("k", v),
            wait=0.1,
        )
    assert computed == []                          # 没有绕过锁计算
    assert cache_env.get("k") is None              # 也没写任何东西
    assert cache_env._locks[cache_store.lock_name("t", "i")][0] == holder   # 别人的锁没被碰


def test_single_flight_busy_is_retryable(cache_env):
    """CacheBusy 是可重试状态:对方算完释放锁后,重试直接命中缓存、不再重复计算。"""
    lkey = cache_store.lock_name("t", "i")
    token = cache_env.acquire_lock(lkey, ttl=30)
    with pytest.raises(CacheBusy):
        single_flight(
            kind="t", identity="i",
            read=lambda: cache_env.get("k"),
            compute=lambda: "自己算的",
            write=lambda v: cache_env.set("k", v),
            wait=0.05,
        )
    cache_env.set("k", "对方算的")                 # 对方完成:写缓存 → 释放锁
    cache_env.release_lock(lkey, token)

    computed: list[int] = []
    value, hit = single_flight(
        kind="t", identity="i",
        read=lambda: cache_env.get("k"),
        compute=lambda: (computed.append(1), "自己算的")[1],
        write=lambda v: cache_env.set("k", v),
        wait=0.05,
    )
    assert (value, hit) == ("对方算的", True)
    assert computed == []


def test_lock_renewer_keeps_lock_alive_past_ttl(cache_env):
    """长计算:后台续期让锁活过原始 TTL(renew 校验持有者令牌)。"""
    lkey = cache_store.lock_name("t", "long")
    token = cache_env.acquire_lock(lkey, ttl=0.4)
    renewer = LockRenewer(cache_env, [(lkey, token)], 0.4, interval=0.05)
    renewer.start()
    try:
        time.sleep(0.25)                                            # 超过原始 TTL
        assert cache_env.renew_lock(lkey, token, 0.4) is True       # 锁仍是我们的
        assert cache_env.renew_lock(lkey, "别人的令牌", 0.4) is False  # 令牌不对续不了
    finally:
        renewer.stop()


def test_single_flight_picks_up_value_written_while_waiting(cache_env):
    """等锁期间别人算好了:持锁后的双检读到结果,自己不再计算(算命中)。"""
    token = cache_env.acquire_lock(cache_store.lock_name("t", "i"), ttl=30)

    def _release_soon():
        time.sleep(0.05)
        cache_env.set("k", "别人算的")
        cache_env.release_lock(cache_store.lock_name("t", "i"), token)

    threading.Thread(target=_release_soon, daemon=True).start()
    computed: list[int] = []
    value, hit = single_flight(
        kind="t", identity="i",
        read=lambda: cache_env.get("k"),
        compute=lambda: (computed.append(1), "自己算的")[1],
        write=lambda v: cache_env.set("k", v),
        wait=2.0,
    )
    assert (value, hit) == ("别人算的", True)
    assert computed == []


# ---- 写入重试 ----

def test_write_with_retry_retries_transient_failure(cache_env):
    real_set = cache_env.set
    calls = {"n": 0}

    def flaky(key, value):
        calls["n"] += 1
        if calls["n"] < 3:
            raise CacheWriteFailed("瞬时失败")
        real_set(key, value)

    cache_env.set = flaky                # type: ignore[method-assign]
    write_with_retry(cache_env, "k", "v", attempts=3)
    assert calls["n"] == 3 and cache_env.get("k") == "v"


def test_write_with_retry_gives_up_after_attempts(cache_env):
    def always_fail(key, value):
        raise CacheWriteFailed("一直失败")

    cache_env.set = always_fail          # type: ignore[method-assign]
    with pytest.raises(CacheWriteFailed):
        write_with_retry(cache_env, "k", "v", attempts=2)


def test_write_with_retry_does_not_retry_oom(cache_env):
    calls = {"n": 0}

    def oom(key, value):
        calls["n"] += 1
        raise CacheOutOfMemory("used memory > maxmemory")

    cache_env.set = oom                  # type: ignore[method-assign]
    with pytest.raises(CacheOutOfMemory):
        write_with_retry(cache_env, "k", "v", attempts=3)
    assert calls["n"] == 1               # 内存满了重试没有意义,立刻报错
