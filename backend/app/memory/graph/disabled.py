"""禁用实现(总开关关闭时的默认组件)。

存在的意义(§6):**关闭是一种配置状态,不是故障** —— 业务侧拿到的是这个实现,
health 明确说「未启用」而不是报连接错误;它不 import 驱动、不建连接、不持有任何资源。
后续步骤(投影 / 召回)给接口加方法时,这里同步补齐「明确禁用」的返回 ——
绝不假装成功、也绝不抛异常打断聊天。
"""

from __future__ import annotations

from app.memory.graph.base import GraphClient, GraphSwitches

DISABLED_REASON = "未启用(总开关 MEMORY_GRAPH_ENABLED 关闭)"


class DisabledGraphClient(GraphClient):
    name = "disabled"

    def __init__(self, reason: str = DISABLED_REASON,
                 switches: GraphSwitches | None = None) -> None:
        self._reason = reason or DISABLED_REASON
        self.switches = switches or GraphSwitches(False, False, False, False)

    def connect(self) -> bool:
        return False

    def health(self) -> dict:
        return {
            "client": self.name,
            "enabled": False,
            "available": False,
            "reason": self._reason,
        }

    def close(self) -> None:
        return None
