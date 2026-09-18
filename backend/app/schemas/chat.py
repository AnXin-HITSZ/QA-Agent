"""对话接口的请求 / 响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., description="用户输入的消息")
    thread_id: str | None = Field(default=None, description="会话线程 ID;传入即续接该会话记忆,留空则后端新开一通对话")


class ChatResponse(BaseModel):
    skill: str | None = Field(default=None, description="命中的 Skill id;None 表示走通用问答")
    content: str = Field(..., description="助手回复文本")
    thread_id: str = Field(..., description="本次会话线程 ID;前端应存下并在后续提问回传以续接记忆")
