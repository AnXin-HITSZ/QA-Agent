"""对话接口的请求 / 响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., description="用户输入的消息")
    thread_id: str | None = Field(default=None, description="会话线程 ID;传入即续接该会话记忆,留空则后端新开一通对话")


class SourceCitation(BaseModel):
    oss_key: str = Field(..., description="原件在知识库中的 key(唯一标识)")
    source: str | None = Field(default=None, description="文件名(展示名)")
    category: str | None = Field(default=None, description="所属分类前缀")
    score: float | None = Field(default=None, description="与问题的相关度(余弦相似度)")
    url: str | None = Field(default=None, description="短时效签名下载 URL(默认 15 分钟过期);OSS 未配置时为 None")


class ChatResponse(BaseModel):
    skill: str | None = Field(default=None, description="命中的 Skill id;None 表示走通用问答")
    content: str = Field(..., description="助手回复文本")
    thread_id: str = Field(..., description="本次会话线程 ID;前端应存下并在后续提问回传以续接记忆")
    sources: list[SourceCitation] = Field(
        default_factory=list,
        description="本次回答引用的知识库来源(带短时效签名 URL);无则为空",
    )
