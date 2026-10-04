"""MySQL 连接:惰性建引擎、短事务会话、可自检、关停时释放连接池(技术方案 §7)。

约束(逐条对应方案):
- 引擎惰性创建:未配置 METERING_MYSQL_URL 时不建任何连接,业务照常跑(计量静默关闭);
- 不建表 / 不改表:建表与升级只走 migrations/ 下手工执行的 SQL(见该目录 README),
  多个 uvicorn worker 启动时不会并发 DDL;
- 连接池保守:每个进程一份池,总量 ≈ workers × (pool + overflow),见方案 §7 的算式;
- 预检 + 回收:pool_pre_ping 检测陈旧连接,pool_recycle 小于 MySQL wait_timeout;
- 超时齐全:连接 / 读 / 写都有超时,任何一次 DB 卡住都不会无限拖住后台补写线程;
- 会话不跨线程共享:每次写入在**当前线程**内开一个短会话,写完即提交关闭;
- 禁止长事务跨外部调用:本模块只服务调用日志,事务里从不发生 OCR / 向量化调用。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from app.config import get_settings

logger = logging.getLogger(__name__)


class MeteringNotConfigured(RuntimeError):
    """未配置 METERING_MYSQL_URL:调用方据此静默关闭计量,不当成故障。"""


_engine = None
_engine_url: str | None = None


def configured() -> bool:
    """是否配了数据库地址(只为「能不能用」判断,不建连接)。"""
    return bool((get_settings().metering_mysql_url or "").strip())


def get_engine():
    """按配置惰性创建引擎;未配置抛 MeteringNotConfigured。"""
    global _engine, _engine_url
    url = (get_settings().metering_mysql_url or "").strip()
    if not url:
        raise MeteringNotConfigured(
            "未配置 METERING_MYSQL_URL:调用日志与费用统计已关闭"
            "(不影响 OCR / 索引 / 检索,见 docs/调用日志与费用统计技术方案.md)"
        )
    if _engine is not None and _engine_url == url:
        return _engine
    if _engine is not None:                      # 配置变了(测试替换 .env)
        try:
            _engine.dispose()
        except Exception:                        # noqa: BLE001 —— 旧池关闭失败不影响新建
            logger.debug("释放旧连接池失败(忽略)", exc_info=True)
    from sqlalchemy import create_engine

    s = get_settings()
    args = {
        # 缺失的库 / 账号等配置错误要立刻暴露,不要静默重试
        "pool_pre_ping": True,
        "pool_recycle": max(60, int(s.metering_mysql_pool_recycle_seconds)),
        "connect_args": {
            "connect_timeout": max(1.0, float(s.metering_mysql_connect_timeout_seconds)),
            "read_timeout": max(1.0, float(s.metering_mysql_read_timeout_seconds)),
            "write_timeout": max(1.0, float(s.metering_mysql_write_timeout_seconds)),
            "charset": "utf8mb4",
        },
    }
    if url.startswith("sqlite"):                 # 仅测试 / 本地演练:SQLite 不支持这些池参数
        args.pop("pool_recycle", None)
        args["connect_args"] = {}
    else:
        args["pool_size"] = max(1, int(s.metering_mysql_pool_size))
        args["max_overflow"] = max(0, int(s.metering_mysql_max_overflow))
    _engine = create_engine(url, **args)
    _engine_url = url
    return _engine


def session_factory():
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


@contextmanager
def session_scope() -> Iterator:
    """一次短事务:成功提交、失败回滚、无论如何关闭(连接归还池)。"""
    factory = session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping() -> tuple[bool, str]:
    """连通性自检:返回 (是否可用, 不可用原因)。绝不抛异常给调用方。"""
    from sqlalchemy import text

    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
        return True, ""
    except MeteringNotConfigured as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 —— 网络 / 认证 / 权限都归为「不可用」
        return False, f"{type(exc).__name__}: {exc}"


def utc_naive() -> datetime:
    """写入用时间:UTC 且不带时区(库列统一 DATETIME(6),UTC 语义由文档与代码保证)。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def dispose() -> None:
    """关停时释放连接池(应用 lifespan / 测试清理调用)。"""
    global _engine, _engine_url
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:                        # noqa: BLE001
            logger.warning("释放 MySQL 连接池失败", exc_info=True)
    _engine = None
    _engine_url = None
