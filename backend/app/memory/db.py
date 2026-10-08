"""长期记忆模块的数据库访问：与计量 / 认证共用同一个 MySQL 引擎、连接池与短事务约定。

一个应用一个库：MYSQL_URL 指向的就是「本应用的数据库」，调用日志、用户、会话目录、
审计、长期记忆都在里面（见 docs/长期记忆系统技术方案.md）。

与认证的关键差异：**记忆是软依赖**。没配库时聊天照常（检索 / 记录全部跳过），
记忆自己的管理接口返回 503 并说明原因（绝不假装「你没有记忆」）。
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Iterator, TypeVar

import anyio

from app.config import get_settings
from app.memory.errors import MemoryNotConfigured
from app.metering import db as _mysql

T = TypeVar("T")


def configured() -> bool:
    """是否配了数据库地址（只做「能不能用」判断，不建连接）。"""
    return bool((get_settings().mysql_url or "").strip())


def enabled() -> bool:
    """长期记忆总开关：显式打开（默认开）且配了数据库。业务侧最便宜的前置判断。"""
    try:
        return bool(get_settings().memory_enabled) and configured()
    except Exception:  # noqa: BLE001 —— 配置异常当关闭，不影响聊天
        return False


def _require_configured() -> None:
    if not configured():
        raise MemoryNotConfigured(
            "未配置数据库（MYSQL_URL）：长期记忆不可用。请按 "
            "docs/长期记忆系统技术方案.md 的部署章节配置后重启。"
        )


@contextmanager
def session_scope() -> Iterator:
    """一次短事务：成功提交、失败回滚、无论如何关闭（连接归还池），与计量 / 认证共用实现。

    铁律（与计量 / 认证一致）：会话不跨线程；事务里绝不发生外部调用
    （LLM 提取 / Embedding / 向量库写入都在事务**之外**，只把结果落库）。
    """
    _require_configured()
    try:
        with _mysql.session_scope() as session:
            yield session
    except _mysql.MeteringNotConfigured as exc:      # 兜底：配置在读的过程中被清空
        raise MemoryNotConfigured(str(exc)) from exc


async def run(fn: Callable[..., T], *args, **kwargs) -> T:
    """在 worker 线程里跑同步 DB 调用：异步路由里**不**让 SQLAlchemy 阻塞事件循环。"""
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


def get_engine():
    """底层引擎（与计量 / 认证同一个）。**只给测试建表用**：应用代码不许拿它执行 DDL。"""
    return _mysql.get_engine()


def utc_naive() -> datetime:
    """写入用时间：UTC 且不带时区（DATETIME(6) 列，UTC 语义由代码保证）。"""
    return _mysql.utc_naive()


def ping() -> tuple[bool, str]:
    """连通性自检（启动日志 / 运维排查用），绝不抛异常。"""
    if not configured():
        return False, "未配置 MYSQL_URL"
    return _mysql.ping()


def dispose() -> None:
    """关停时释放连接池（与计量 / 认证共用池，只释放一次）。"""
    _mysql.dispose()
