"""认证模块的数据库访问:与计量层共用同一个 MySQL 引擎 / 连接池 / 短事务约定。

一个应用一个库:`MYSQL_URL` 指向的就是「本应用的数据库」,调用日志、用户、
会话、会话目录、审计都在里面(见 docs/认证鉴权与用户管理技术方案.md §7)。
不另建第二个连接配置,也不另建第二个池 —— 池总量按「workers × (pool + overflow)」算一次就好。

与计量的关键差异:**认证是硬依赖**。计量没配库就静默关闭(业务照常),认证没配库必须
明确报错(绝不能因为「库连不上」就变成匿名可用)。
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Iterator, TypeVar

import anyio

from app.auth.errors import AuthNotConfigured
from app.config import get_settings
from app.metering import db as _mysql

T = TypeVar("T")


def configured() -> bool:
    """是否配了数据库地址(只做「能不能用」判断,不建连接)。"""
    return bool((get_settings().mysql_url or "").strip())


def _require_configured() -> None:
    if not configured():
        raise AuthNotConfigured(
            "未配置数据库(MYSQL_URL):用户 / 会话 / 会话目录都存 MySQL,"
            "认证接口不可用。请按 docs/认证鉴权与用户管理技术方案.md §10 配置后重启。"
        )


@contextmanager
def session_scope() -> Iterator:
    """一次短事务:成功提交、失败回滚、无论如何关闭(连接归还池),与计量共用同一实现。

    铁律(与计量一致):会话不跨线程;事务里绝不发生外部调用(发信 / 模型 / OSS)。
    发信一律放在事务**之外**(见 service.py:先提交用户行,再发信;发信失败按可重试处理)。
    """
    _require_configured()
    try:
        with _mysql.session_scope() as session:
            yield session
    except _mysql.MeteringNotConfigured as exc:      # 兜底:配置在读的过程中被清空
        raise AuthNotConfigured(str(exc)) from exc


async def run(fn: Callable[..., T], *args, **kwargs) -> T:
    """在 worker 线程里跑同步 DB 调用:异步路由里**不**让 SQLAlchemy 阻塞事件循环。

    session_scope 是同步上下文管理器,所以所有 store / 组合操作都是同步函数,
    统一从这里进线程池(技术方案 §7「数据库操作不得阻塞事件循环」)。
    """
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


def get_engine():
    """底层引擎(与计量同一个)。**只给测试建表用**:应用代码不许拿它执行 DDL。"""
    return _mysql.get_engine()


def utc_naive() -> datetime:
    """写入用时间:UTC 且不带时区(DATETIME(6) 列,UTC 语义由代码保证)。"""
    return _mysql.utc_naive()


def ping() -> tuple[bool, str]:
    """连通性自检(启动日志 / 运维排查用),绝不抛异常。"""
    if not configured():
        return False, "未配置 MYSQL_URL"
    return _mysql.ping()


def dispose() -> None:
    """关停时释放连接池(与计量共用池,只释放一次)。"""
    _mysql.dispose()
