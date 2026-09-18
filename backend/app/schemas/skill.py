"""Skill(SOP)目录项模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SkillMeta(BaseModel):
    id: str = Field(..., description="Skill 唯一标识(缺省用文件名)")
    name: str = Field(..., description="人类可读名称")
    description: str = Field(default="", description="用途简述,供 LLM 选择时判断")
    triggers: list[str] = Field(default_factory=list, description="触发提示词(供目录展示/召回)")


class SopHit(BaseModel):
    """一次 SOP 检索的命中项(供工具格式化给 LLM / 未来回传前端)。"""

    id: str = Field(..., description="命中的 Skill id")
    name: str = Field(..., description="人类可读名称")
    description: str = Field(default="", description="用途简述")
    score: int = Field(default=0, description="相关度打分,越大越相关")
    snippet: str = Field(default="", description="正文中的命中片段")
