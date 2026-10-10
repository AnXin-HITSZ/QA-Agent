"""记忆图:Neo4j 只是**可重建投影**,事实源仍在 MySQL(§7;方案见 docs/记忆图索引技术方案.md)。

定位:实体 / 别名 / 关系 / 事件在 MySQL 里保存(FK 不用,事实绑定来源与记忆版本),
Neo4j 里放的只是「同一份事实的图形态」,用于遍历、事件链接与图候选召回;
它任何时候都可以从 MySQL 重建(管理命令),不一致以 MySQL 为准。

模块分工(自下而上,业务层不越级):
    models.py       领域数据类 + 名称匹配键(name_key),不碰库、不碰 Neo4j
    tables.py       七张 MySQL 表(SQLAlchemy 声明;迁移 0009 与之逐项一致)
    repo.py         全部 SQL(读写都带 user_id + scope;幂等 / 版本 / 代次)
    extract.py      图提取协议 mem-graph-extract/2:把已入库事实整理成图元素(带事实编号)
    base.py         开关矩阵收敛(resolve_switches)与 GraphClient 接口契约

统一的图组件接口(禁止散落 Neo4j 查询):
    get_graph_client()            进程内单例(生产入口;lifespan 连接 / 收尾)
    build_graph_client(settings)  按给定配置装配(测试 / 管理命令用)
    DisabledGraphClient           总开关关闭时的默认实现:不 import 驱动、不连接
    Neo4jGraphClient              懒加载官方驱动的实现(bolt)

开关矩阵(§6;一律「改环境变量 + 重启进程」生效,没有运行时热切换 / 请求级旁路):

    MEMORY_GRAPH_ENABLED  写+投影  召回   后台    解释
    false(默认)           -        -      -       全关:不加载驱动、不连接,现有记忆一字不变
    true  WRITE+WORKER    ✓        ✗      ✓       只建图不召回(构建 / 回填固定图数据)
    true  SEARCH(写关)     ✗        ✓      -       读侧实验:召回固定图数据,图不再增长
    true  三个子开关全开    ✓        ✓      ✓       全开

    子开关单独为 true 而总开关为 false 时按 false 处理(resolve_switches 统一收敛)。

关键语义:
- 「关闭」是配置状态,不是故障:health 明确说「未启用」而不是报连接错误;
- 关闭期间**不丢失删除意图**:彻底清除 / 删除的图侧清理先登记在 MySQL 台账
  (memory_ops,复用既有机制),重新启用后由 Worker 补做 —— 不在这一层;
- 「无结果」「未同步」「失败」与「关闭」在诊断里分开说,不混为一谈(§12)。
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
