"""在途生成登记:删会话时等这通对话正在跑的生成收尾,别刚删完又被写回去。

chat / chat/stream 在生成期间登记(thread_id 计数),结束时注销 —— 包含异常退出与客户端
主动断开。删除流程据此等一小会儿(WAIT_SECONDS),让在途那次写完再抹 Redis。

**这是单进程内的保护**:多 worker 部署时,别的 worker 正在跑的生成这里看不见。那种情况
由「目录先置 deleting」兜住 —— 新请求当场进不来,在途那次写完就结束了;万一它恰好在
adelete_thread 之后落盘,残留键会在下次启动对账时清掉(deleting 行会被重删一遍)。
宁可多删一次,不可留下「用户以为删了、其实还在」的正文。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)

WAIT_SECONDS = 5.0          # 等在一次对话上的上限:超时就继续删,并留一条告警

_sessions: dict[str, int] = {}              # thread_id -> 正在跑的次数
_idle: dict[str, asyncio.Event] = {}        # thread_id -> 「跑完了」的信号


@contextlib.asynccontextmanager
async def track(thread_id: str):
    """登记一次生成。退出时注销(含被取消 / 抛异常)。"""
    _sessions[thread_id] = _sessions.get(thread_id, 0) + 1
    _idle.pop(thread_id, None)              # 新一轮开始:上一轮的「空闲」信号作废
    try:
        yield
    finally:
        left = _sessions.get(thread_id, 0) - 1
        if left > 0:
            _sessions[thread_id] = left
        else:
            _sessions.pop(thread_id, None)
            _idle.setdefault(thread_id, asyncio.Event()).set()


def active(thread_id: str) -> int:
    """这条线程上还有几次生成在跑(测试与排查用)。"""
    return _sessions.get(thread_id, 0)


async def wait_idle(thread_id: str, timeout: float = WAIT_SECONDS) -> bool:
    """等这条线程上的生成跑完。返回是否真的等到了(超时返回 False,调用方照常往下走)。"""
    if not _sessions.get(thread_id):
        return True
    event = _idle.setdefault(thread_id, asyncio.Event())
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return True
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("删除会话:%s 上仍有生成在跑,等待 %.1fs 超时,继续删除",
                       thread_id, timeout)
        return False


def reset() -> None:
    """清空登记(测试用:免得跨用例留下痕迹)。"""
    _sessions.clear()
    _idle.clear()
