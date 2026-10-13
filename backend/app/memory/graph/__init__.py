"""存量通用图的撤回与清理基础设施。通用图抽取和问答召回已经删除。
新事务图实现在 app.matters.projection，权威数据在 app.matters.tables。
本包仅保留历史数据隔离、删除意图、对账和驱动生命周期，不能创建新图记忆。
关闭总开关时不连接 Neo4j，清理意图由原台账保存，开启后继续执行。
"""

from __future__ import annotations

import threading

from app.config import Settings, get_settings
from app.memory.graph.base import GraphClient, GraphSwitches, resolve_switches
from app.memory.graph.disabled import DISABLED_REASON, DisabledGraphClient
from app.memory.graph.neo4j_client import Neo4jGraphClient

__all__ = [
    "DISABLED_REASON", "DisabledGraphClient", "GraphClient", "GraphSwitches",
    "Neo4jGraphClient", "build_graph_client", "get_graph_client", "reset_graph_client",
    "resolve_switches",
]

_lock = threading.Lock()
_client: GraphClient | None = None


def build_graph_client(settings: Settings | None = None) -> GraphClient:
    """按配置装配图组件(**不连接**;连接发生在显式的 connect(),由 lifespan / 命令调用)。"""
    settings = settings or get_settings()
    switches = resolve_switches(settings)
    if not switches.master:
        return DisabledGraphClient(switches=switches)
    return Neo4jGraphClient(settings)


def get_graph_client() -> GraphClient:
    """进程内单例。配置切换靠重启进程,所以缓存是安全的(不提供运行时替换)。"""
    global _client
    with _lock:
        if _client is None:
            _client = build_graph_client()
        return _client


def reset_graph_client() -> None:
    """丢掉单例(测试用;**不关闭**旧实例 —— 关闭由持有方显式调用)。"""
    global _client
    with _lock:
        _client = None
