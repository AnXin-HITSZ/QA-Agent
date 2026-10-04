"""HTTP 用量探针:挂在 httpx 响应钩子上,收集「供应商实际报了多少 token」。

为什么需要它:langchain-openai 的 embed_documents 只把 data(向量)返回给上层,
响应里的 usage(prompt_tokens)在 SDK 内部就被丢掉了;而官方用量是估算费用最可信的
来源(方案 §4:优先用供应商返回的 usage,不伪造字符→token 换算)。

各 SDK 的重试都发生在同一个 httpx 客户端上,所以钩子还能观测到**真实发出的请求次数**
(收到几次响应就是几次尝试)—— 这正是「在可观测范围内逐次记录真实调用」的依据。

线程局部收集:一次调用期间只有本线程的钩子会写入,互不干扰;收集窗口结束后立即取走。
钩子里只读 usage,不落任何请求体 / 密钥。
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

# 供应商用量里可能出现的 token 字段(OpenAI 兼容 / DashScope 兼容模式都覆盖)
_TOKEN_KEYS = ("prompt_tokens", "input_tokens", "total_tokens")


class UsageProbe:
    """一次安装、长期复用:钩子里按线程把响应摘要写进收集窗口。"""

    def __init__(self) -> None:
        self._local = threading.local()

    def hook(self, response) -> None:  # httpx.Response
        sink = getattr(self._local, "sink", None)
        if sink is None:
            return
        entry: dict = {"status": int(response.status_code)}
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 —— 非 JSON / 未读 body:只记状态码
            payload = None
        if isinstance(payload, dict):
            usage = payload.get("usage")
            if isinstance(usage, dict):
                entry["usage"] = {
                    k: v for k, v in usage.items()
                    if k in _TOKEN_KEYS and isinstance(v, (int, float))
                }
        sink.append(entry)

    @contextmanager
    def collecting(self) -> Iterator[list[dict]]:
        """收集窗口:窗口内本线程收到的 HTTP 响应都记进返回的列表。"""
        sink: list[dict] = []
        previous = getattr(self._local, "sink", None)
        self._local.sink = sink
        try:
            yield sink
        finally:
            self._local.sink = previous


def attempts(entries: list[dict]) -> int:
    """可观测到的真实请求次数(SDK 内部重试也算:收到一次响应就是一次尝试)。"""
    return len(entries)


def prompt_tokens(entries: list[dict]) -> int | None:
    """汇总供应商报的输入 token;一次都没报返回 None(不猜)。"""
    total = 0
    seen = False
    for entry in entries:
        usage = entry.get("usage") or {}
        for key in _TOKEN_KEYS:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                total += int(value)
                seen = True
                break
    return total if seen else None


NULL_PROBE = UsageProbe()   # 未接探针时的空实现(collecting 返回空列表)


class ReadProbe:
    """包装 single_flight 的 read 回调,区分「直接读到缓存」与「等锁 / 双检后复用」。

    single_flight 只回一个 hit 布尔,三种命中在它那里是同一个 True;缓存统计要分开:
    - 第一次 read 就命中        → 真实缓存命中(省下一次外部调用)
    - read 被调用多次才命中     → 等锁 / 双检期间复用了别人的结果(同样没有外部调用,
                                  但不是「缓存里本来就有」,口径不同,单独记 shared)
    不改 single_flight 的契约,只在外层观察它调用 read 的次数。
    """

    def __init__(self, read) -> None:
        self._read = read
        self.calls = 0
        self.hits = 0

    def __call__(self):
        self.calls += 1
        value = self._read()
        if value is not None:
            self.hits += 1
        return value

    @property
    def read_hit(self) -> bool:
        return self.hits > 0 and self.calls <= 1


def classify(probe: ReadProbe, hit: bool | None, *, attempted: bool = False) -> dict[str, int]:
    """(探测结果, single_flight 的 hit) → 缓存事件计数,单位 = 1 次调用。

    hit=None 表示这次调用没走完(异常中断):attempted=True(已经发起了计算)记一条
    未命中 —— 确实没命中、也确实算了;attempted=False(如抢锁失败,根本没算)不计入
    任何一类,由任务错误明细说明,免得把「没算」算成「算了」。
    """
    if hit is None:
        return {"hit": 0, "miss": 1, "shared": 0} if attempted else {"hit": 0, "miss": 0, "shared": 0}
    if not hit:
        return {"hit": 0, "miss": 1, "shared": 0}
    if probe.read_hit:
        return {"hit": 1, "miss": 0, "shared": 0}
    return {"hit": 0, "miss": 0, "shared": 1}
