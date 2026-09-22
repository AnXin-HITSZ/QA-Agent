"""待办清单:全局一份的轻量存储 + 给 Agent 的只读渲染。

存储复用现有 Redis(与 checkpointer 同一实例,不同键空间),用单个字符串键存一份
JSON 数组;CRUD 即读改写整份,内部单用户量级足够。Redis 未配置 / 连不上则整个特性
优雅降级(接口回 enabled=false / 503),绝不拖垮聊天与 RAG。
"""

from app.todos.store import TodoStore, create_todo_store, format_for_prompt

__all__ = ["TodoStore", "create_todo_store", "format_for_prompt"]
