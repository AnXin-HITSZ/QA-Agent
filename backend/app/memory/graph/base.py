"""图组件接口(§7):禁用实现与 Neo4j 实现共用的最小契约。

这个文件只放**与实现无关**的东西:有效开关解析(GraphSwitches)、接口本身(GraphClient)。
接口按「先生命周期,后能力」演进:投影(写)与召回(读)的方法在各自步骤加入,两个实现
同时补齐 —— 禁用实现返回明确的「未启用」状态,不抛异常、不假装成功(§6:关闭是一种
配置状态,不是故障)。

约定:
- **绝不抛**:connect / health / close 都不向调用方抛异常,失败折进 health 的
  {available: False, reason: ...} —— 启动与聊天不因图故障受影响(§6/§18);
- **懒加载**:Neo4j 实现只在 connect() 里 import 驱动;总开关关闭时连 import 都不发生,
  「全关 ⇒ 零连接」由测试守卫(见 tests/test_memory_graph_switches.py);
- **阻塞调用由调用方放进线程**(同步接口,经 anyio.to_thread 调),不阻塞事件循环(§7)。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class GraphSwitches:
    """一份**已按总开关收敛**的有效开关(子开关在总开关关闭时一律为 False)。

    §6 要求说清「有效的开关矩阵」,收敛只做一次、只在这一处:
    读取开关的代码不许再看 settings,一律用这里的值,避免两处判定漂移。
    """

    master: bool      # MEMORY_GRAPH_ENABLED
    write: bool       # 有效写侧(提取 + 投影)
    search: bool      # 有效读侧(回答检索走图通道)
    worker: bool      # 有效后台(图同步 / 删除 / 对账由 Worker 执行)


def resolve_switches(settings: Settings) -> GraphSwitches:
    """把配置解析成有效开关(唯一的收敛点;纯函数,便于测试矩阵)。"""
    master = bool(settings.memory_graph_enabled)
    return GraphSwitches(
        master=master,
        write=master and bool(settings.memory_graph_write_enabled),
        search=master and bool(settings.memory_graph_search_enabled),
        worker=master and bool(settings.memory_graph_worker_enabled),
    )


class GraphClient(ABC):
    """统一图组件接口:投影(Neo4j 里的可重建数据)与图召回都从这里进出。

    不在这里放 Cypher / 驱动类型:实现细节(参数化查询、约束、批次)进实现类,
    业务侧(search / worker / service)只依赖这个接口 —— 禁止散落 Neo4j 查询(§7)。
    """

    name: str = "graph"

    #: 有效开关(装配时解析一次;读侧 / 写侧代码用它判断,不再各自读 settings)
    switches: GraphSwitches

    def publish(self, data: dict) -> None:
        raise RuntimeError("图投影未启用")

    def recall(self, data: dict, *, query: str) -> dict:
        return {"enabled": False, "candidates": [], "paths": []}

    @abstractmethod
    def connect(self) -> bool:
        """建立连接并探活(同步、可能阻塞;调用方放线程)。**绝不抛**;返回可用与否。"""

    @abstractmethod
    def health(self) -> dict:
        """当前状态快照(健康检查 / 启动日志 / 管理命令用)。**绝不抛**。

        结构(实现可以加字段,这几个键名固定):
            client     实现名(disabled / neo4j)
            enabled    总开关意义上的启用(disabled 恒为 False)
            available  现在是否真的可用(禁用 = False,连接失败 = False)
            reason     不可用的原因(禁用原因 / 异常摘要;正常时为空串)
        """

    @abstractmethod
    def close(self) -> None:
        """释放连接(幂等;未连接时是 no-op)。**绝不抛**。"""
