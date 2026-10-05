"""历史对话接口的响应模型(列表 / 详情)。"""

from __future__ import annotations

from pydantic import BaseModel, Field
from app.schemas.image import SopImageReference


class ConversationMessage(BaseModel):
    images: list[SopImageReference] = Field(default_factory=list)
    role: str = Field(..., description="user 或 assistant")
    content: str = Field(..., description="消息文本")


class ConversationMatch(BaseModel):
    role: str = Field(..., description="命中的是提问(user)还是回答(assistant)")
    snippet: str = Field(..., description="命中处上下文片段,被截断的一端带 …")
    count: int = Field(..., description="这通对话里命中关键词组的消息条数")


class ConversationSummary(BaseModel):
    thread_id: str = Field(..., description="会话线程 ID,即列表项的钥匙")
    title: str = Field(..., description="标题:首条用户提问截断")
    message_count: int = Field(..., description="可回放的消息条数(用户提问 + 助手回答)")
    updated_at: str | None = Field(default=None, description="最新 checkpoint 的 ISO 时间;用于展示与排序")
    match: ConversationMatch | None = Field(default=None, description="带 q 搜索时的命中说明;未搜索为 null")


class ConversationList(BaseModel):
    enabled: bool = Field(..., description="Redis 跨轮记忆是否配置启用;false 表示未配置(单轮模式)")
    degraded: bool = Field(default=False, description="已启用但本次读取失败(超时/Redis 错误);列表仍来自 MySQL 目录,但条目缺少消息数")
    items: list[ConversationSummary] = Field(default_factory=list, description="会话摘要,最近活跃在前")
    total: int = Field(default=0, description="自己的会话总数(带 q 时是本次检索窗口内的命中数)")
    offset: int = Field(default=0, description="本页起始偏移(回显请求参数)")
    limit: int = Field(default=0, description="本页大小(回显请求参数)")


class ConversationDetail(BaseModel):
    thread_id: str = Field(..., description="会话线程 ID")
    messages: list[ConversationMessage] = Field(default_factory=list, description="回放用的 Q&A 文本序列")
