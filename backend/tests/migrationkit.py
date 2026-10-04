"""迁移文件的公共读法:test_metering_migration.py 与 test_metering_mysql.py 共用。

迁移是 backend/migrations/*.up.sql / *.down.sql(纯 SQL、由人手工执行,约定见该目录 README)。
本模块只做两件事 —— 按版本列文件、把一份 .sql 拆成可逐条执行的语句;把 SQL 解析成结构
(列 / 键 / 索引)的部分留在 test_metering_migration.py,因为它只服务那一个用例。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

BACKEND = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = BACKEND / "migrations"

# 与 D:\Projects\personal-site\backend\migrations 相同的约定:
# <四位版本>_<名称>.up.sql / .down.sql,版本只增不复用。
_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.(up|down)\.sql$")


class Migration(NamedTuple):
    version: int
    name: str
    path: Path


def migrations(direction: str) -> list[Migration]:
    """按版本升序列出某个方向的迁移文件;文件名不合规 / 版本重复直接报错。"""
    assert direction in ("up", "down"), direction
    out: list[Migration] = []
    for path in MIGRATIONS_DIR.glob(f"*.{direction}.sql"):
        m = _FILENAME.match(path.name)
        assert m, f"迁移文件名不合规:{path.name}(应为 <四位版本>_<名称>.{direction}.sql)"
        out.append(Migration(int(m.group(1)), m.group(2), path))
    out.sort(key=lambda item: item.version)
    versions = [item.version for item in out]
    assert len(versions) == len(set(versions)), f"版本号重复:{versions}"
    return out


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def statements(sql: str) -> list[str]:
    """把一份 .sql 拆成可逐条执行的语句。

    - 先去掉 `--` 行注释(不在字符串字面量里的);
    - 再按分号切,但跳过字符串字面量里的分号 —— 列注释里就有「用量;NULL = 未取得」;
    - 空白语句丢弃;末尾没带分号的最后一条也算一条(本仓库的文件都带分号,这里只是兜底)。

    本仓库的 SQL 不使用反斜杠转义(没有 \\' 之类):真写了会解析错并在比对时显式报错,
    不会静默切错。
    """
    body = _strip_line_comments(sql)
    out: list[str] = []
    buf: list[str] = []
    in_quote = False
    for ch in body:
        if ch == "'":
            in_quote = not in_quote          # '' 转义会翻转两次,自动还原
        elif ch == ";" and not in_quote:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _strip_line_comments(sql: str) -> str:
    """去掉 `-- 到行尾` 的注释(字符串字面量里的 `--` 原样保留),换行保留。"""
    out: list[str] = []
    in_quote = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            in_quote = not in_quote
            out.append(ch)
            i += 1
            continue
        if not in_quote and ch == "-" and sql.startswith("--", i):
            newline = sql.find("\n", i)
            if newline < 0:
                break
            out.append("\n")
            i = newline + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)
