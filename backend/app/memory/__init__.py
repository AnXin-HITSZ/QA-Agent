"""长期记忆：跨会话记住用户的稳定事实（方案见 docs/长期记忆系统技术方案.md）。

对业务只暴露很少几个入口：
    from app.memory import service          # 检索 / 入队 / 用户增删改
    from app.memory import worker           # 后台提取线程（lifespan 起停）

分层（与计量 / 认证同一套姿态）：
    tables.py   MySQL 表结构（事实源；建表 SQL 在 backend/migrations/0004_*.up.sql）
    repo.py     全部 SQL 读写（同步、短事务）
    db.py       连接与线程池封装（与计量 / 认证共用同一个 MySQL）
    models.py   领域数据类与正文规范化（纯函数，不碰库）
    errors.py   异常类型（没配依赖 ≠ 猜着跑，见模块内说明）
    extract.py  LLM 提取 + 校验（只产出候选事实，不写库）
    maintain.py ADD/UPDATE/DELETE/NOOP 维护决策与应用
    vector.py   Qdrant 记忆索引（可重建；事实源永远是 MySQL）
    text.py     中文分词（cjk-bigram-v1）与 BM25 打分
    search.py   混合检索：向量 + 中文 BM25 → RRF →（可选）重排序
    rerank.py   重排序客户端（qwen3.7-text-rerank，超时按未知计费）
    llm.py      记忆用的聊天模型（复用 LLM_* 配置，温度 0）
    worker.py   后台任务线程：认领 → 提取 → 维护 → 收尾
    service.py  对外编排（检索注入对话 / 入队 / 用户增删改 / 重建）
    graph/      记忆图：Neo4j 只是**可重建投影**，事实源仍是 MySQL（总开关默认关；
                全关时不加载驱动、不连接，见该包内的开关矩阵）

三条硬约束：
- **绝不拖垮聊天**：记忆的检索、入队、提取全部失败也不影响回答本身；
- **MySQL 是事实源**：Qdrant 只是可重建索引，删了能重建，不一致以 MySQL 为准；
- **不落敏感正文到日志**：日志里只出现条数 / id 前缀 / 错误摘要，不出现记忆正文。
"""

from app.memory.errors import (
    MemoryConflict, MemoryDisabled, MemoryExtractionError, MemoryNotConfigured, MemoryNotFound,
    MemoryStaleGeneration,
)
from app.memory.models import ExtractionOutcome, MemoryHistory, MemoryItem, MemoryJob

__all__ = [
    "ExtractionOutcome", "MemoryConflict", "MemoryDisabled", "MemoryExtractionError",
    "MemoryHistory", "MemoryItem", "MemoryJob", "MemoryNotConfigured", "MemoryNotFound",
    "MemoryStaleGeneration",
]
