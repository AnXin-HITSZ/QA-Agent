"""历史对话接口的响应模型(列表 / 详情)。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ConversationMessage(BaseModel):
    role: str = Field(..., description="user 或 assistant")
    content: str = Field(..., description="消息文本")


class ConversationSummary(BaseModel):
    thread_id: str = Field(..., description="会话线程 ID,即列表项的钥匙")
    title: str = Field(..., description="标题:首条用户提问截断")
    message_count: int = Field(..., description="可回放的消息条数(用户提问 + 助手回答)")
    updated_at: str | None = Field(default=None, description="最新 checkpoint 的 ISO 时间;用于展示与排序")


class ConversationList(BaseModel):
    enabled: bool = Field(..., description="Redis 跨轮记忆是否配置启用;false 表示未配置(单轮模式)")
    degraded: bool = Field(default=False, description="已启用但本次读取失败(超时/Redis 错误);true 时 items 恒空,前端提示重试")
    items: list[ConversationSummary] = Field(default_factory=list, description="会话摘要,最近活跃在前")


class ConversationDetail(BaseModel):
    thread_id: str = Field(..., description="会话线程 ID")
    messages: list[ConversationMessage] = Field(default_factory=list, description="回放用的 Q&A 文本序列")
