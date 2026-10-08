"""调用上下文(谁发起的这次调用):用 contextvar 传,不改动调用链签名。

OCR / 向量化发生在很深的调用栈里(索引线程 → 摄取 → 逐页提取 → 缓存 → 供应商),
把 job_id / document_id / oss_key / 页码一路显式透传会污染每个函数的签名。这里用
contextvar 在业务入口处「就地标注」,计量层在发请求时读取:

- 每个索引任务开头标注 purpose=document_index + job_id(作用域覆盖整批文件);
- 每个文件登记身份后标注 document_id + oss_key(作用域覆盖该文件的提取与向量化);
- 每页识别前标注 page_no(作用域覆盖该页);
- 检索(聊天)默认 purpose=query,不标注文件信息;
- 长期记忆的每次外部调用(提取 / 维护决策 / 检索)在入口标注 memory_* 用途(见下面常量)。

contextvar 是线程 / 协程隔离的:两个索引任务、索引线程与请求线程互不串味。
标注失败不影响业务 —— 计量层拿不到上下文时照常记录,只是归属字段为空。
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Iterator

PURPOSE_INDEX = "document_index"
PURPOSE_QUERY = "query"
# 长期记忆的三段用途(调用日志里按它区分「记忆在干什么」):
# 提取(对话 → 事实)、维护决策(新旧记忆 ADD/UPDATE/DELETE)、检索(回答问题前的召回)。
# 记忆写入 / 查询的 Embedding 与 rerank 都沿用这三者之一,不再另设。
PURPOSE_MEMORY_EXTRACT = "memory_extract"
PURPOSE_MEMORY_MAINTENANCE = "memory_maintenance"
PURPOSE_MEMORY_SEARCH = "memory_search"


@dataclass(frozen=True)
class CallContext:
    """一次调用发生时的归属信息(全部可空:拿不到就如实留空,不猜)。"""

    purpose: str = PURPOSE_QUERY
    job_id: str | None = None
    document_id: str | None = None
    oss_key: str | None = None
    page_no: int | None = None

    def with_(self, **changes) -> "CallContext":
        return replace(self, **changes)


_current: contextvars.ContextVar[CallContext] = contextvars.ContextVar(
    "metering_call_context", default=CallContext()
)


def current() -> CallContext:
    return _current.get()


@contextmanager
def use(ctx: CallContext) -> Iterator[CallContext]:
    """在块内使用给定上下文,退出时恢复(嵌套安全)。"""
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


@contextmanager
def bind(**changes) -> Iterator[CallContext]:
    """在块内追加标注,退出时恢复(嵌套安全;循环里逐个文件标注也用它)。

    必须配对恢复:索引任务 / 请求都跑在会被复用的线程上,不恢复会把上一个任务的文件
    信息带给下一个任务。
    """
    token = _current.set(_current.get().with_(**changes))
    try:
        yield _current.get()
    finally:
        _current.reset(token)
