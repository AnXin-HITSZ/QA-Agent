"""数据库不可用时的本地补写目录:故障恢复队列,不是第二份可查询日志库(技术方案 §8)。

规则:
- 只有「MySQL 写失败」的记录才会落到这里;应用正常时这个目录恒为空;
- 一个事件一个文件(`<event_id>.json`,原子写),文件名 = 事件 id → 天然去重;
- 多 worker 安全:领取靠 os.rename 的原子性(改名成带 pid/uuid 的 claim 名,只有一个
  进程能成功);写库成功后才删除 claim 文件,中途崩溃由「陈旧 claim 回收」放回队列;
- 损坏文件不静默丢弃:扫描时告警并保留原文件,交人工处理;
- 目录只被本模块与运维读取,不对外提供查询接口(方案明确不把它当主存储)。
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.rag.localfs import atomic_write_json, read_json   # 与任务状态共用同一套原子读写

logger = logging.getLogger(__name__)

CLAIM_MARK = ".claim-"
STALE_CLAIM_SECONDS = 15 * 60     # 领取后超过这么久没写完,视为崩溃遗留,放回队列
MAX_SCAN = 5000                   # 单轮扫描上限(防止异常时把内存 / IO 打满)


def pending_dir() -> Path:
    return Path(get_settings().metering_pending_dir)


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def record_path(event_id: str, directory: Path | None = None) -> Path:
    return (directory or pending_dir()) / f"{event_id}.json"


def write(payload: dict, *, directory: Path | None = None) -> Path | None:
    """把一条记录落到补写目录。成功返回路径;失败返回 None 并告警(不静默丢失)。"""
    d = directory or pending_dir()
    event_id = str(payload.get("event_id") or "")
    if not event_id:
        logger.warning("补写记录缺少 event_id,无法落盘(丢弃前请检查记录构造)")
        return None
    path = record_path(event_id, d)
    try:
        atomic_write_json(path, payload)
        return path
    except Exception as exc:  # noqa: BLE001 —— 磁盘满 / 权限 / 路径非法
        logger.error("调用日志补写文件写入失败(该条记录丢失):%s", exc)
        return None


def is_claim(path: Path) -> bool:
    return CLAIM_MARK in path.name


def pending_files(directory: Path | None = None, *, limit: int = MAX_SCAN) -> list[Path]:
    """待写文件(不含 claim);按修改时间从旧到新,先补最老的。"""
    d = directory or pending_dir()
    if not d.is_dir():
        return []
    out = [p for p in d.glob("*.json") if p.is_file()]
    out.sort(key=lambda p: p.stat().st_mtime)
    return out[: max(1, limit)]


def claim_files(directory: Path | None = None, *, limit: int = MAX_SCAN) -> list[Path]:
    d = directory or pending_dir()
    if not d.is_dir():
        return []
    out = [p for p in d.glob(f"*{CLAIM_MARK}*") if p.is_file()]
    out.sort(key=lambda p: p.stat().st_mtime)
    return out[: max(1, limit)]


def claim(path: Path) -> Path | None:
    """领取一个待写文件:改成唯一 claim 名。返回 None = 已被别的进程领走。"""
    target = path.with_name(f"{path.name}{CLAIM_MARK}{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        os.rename(path, target)
        return target
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("领取补写文件失败(可能被其它进程领走):%s", exc)
        return None


def original_name(claim_path: Path) -> Path:
    """claim 文件 → 原文件名(放回队列用)。"""
    return claim_path.with_name(claim_path.name.split(CLAIM_MARK, 1)[0])


def finish(claim_path: Path) -> None:
    """数据库已提交:删除 claim 文件(必须提交成功后调用)。"""
    try:
        claim_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("删除补写 claim 失败(该记录已入库,残留文件将在下轮被幂等重放):%s", exc)


def release(claim_path: Path) -> None:
    """写库失败:把 claim 放回队列,等下一轮重试。"""
    back = original_name(claim_path)
    try:
        os.replace(claim_path, back)
    except OSError as exc:
        logger.warning("补写文件放回队列失败(保留为 claim,稍后由陈旧回收处理):%s", exc)


def sweep_stale_claims(directory: Path | None = None, *,
                       max_age_seconds: float = STALE_CLAIM_SECONDS) -> int:
    """回收陈旧 claim(进程崩溃遗留):放回待写队列,幂等写入保证不会重复。"""
    n = 0
    cutoff = _now_ts() - max(60.0, float(max_age_seconds))
    for path in claim_files(directory):
        try:
            if path.stat().st_mtime < cutoff:
                release(path)
                n += 1
        except OSError:
            continue
    if n:
        logger.warning("回收 %d 个陈旧的补写 claim 文件(上次写入中途退出)", n)
    return n


def count(directory: Path | None = None) -> dict:
    """补写目录现状:待写 / 领取中 / 损坏(供状态接口展示「完整性」)。"""
    d = directory or pending_dir()
    return {"pending": len(pending_files(d, limit=MAX_SCAN)),
            "claimed": len(claim_files(d, limit=MAX_SCAN)),
            "dir": str(d)}


def read_payload(path: Path) -> dict | None:
    """读一条补写记录;损坏返回 None(调用方保留原文件并告警,不静默删)。"""
    data = read_json(path, default=None)
    if not isinstance(data, dict) or not data.get("event_id"):
        return None
    return data
