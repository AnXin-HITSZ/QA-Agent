"""本地 JSON 读写:OCR 缓存 / 任务状态 / 索引版本指针共用的小工具。

用途一致的三个约束(三处都踩得住):
- 原子写:先写临时文件再 os.replace,进程被杀不会留下半截 JSON;
- 损坏容忍:读失败返回默认值并告警,不因为一个坏文件让整条链路崩;
- 不使用数据库:这些目录只放缓存、任务与指针,随时可重建(见方案 §10)。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_REPLACE_RETRIES = 5      # Windows 上目标文件被读句柄短暂占用时 os.replace 会拒绝访问


def atomic_write_json(path: Path, payload) -> None:
    """原子写 JSON(临时文件 + os.replace),UTF-8、不转义中文。

    Linux 下 os.replace 本身原子且不会因读者失败;Windows 下如果目标正被别的进程 /
    线程打开(杀毒、编辑器、并发读),replace 会报 WinError 5 —— 重试几次即可,
    这正是"状态被频繁改写 + 前端高频轮询"的场景。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_RETRIES - 1:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default=None):
    """读 JSON;不存在返回 default,损坏则告警并返回 default。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except Exception as exc:
        logger.warning("读取 %s 失败:%s", path, exc)
        return default
    try:
        return json.loads(text)
    except Exception as exc:
        logger.warning("解析 %s 失败(按默认值处理):%s", path, exc)
        return default
