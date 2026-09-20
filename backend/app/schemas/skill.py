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


class SopSummary(BaseModel):
    """SOP 列表项(不含正文,供前端列表页渲染卡片)。"""

    id: str = Field(..., description="SOP 唯一标识,即 OSS 内文件名 <id>.md")
    name: str = Field(..., description="人类可读名称")
    description: str = Field(default="", description="用途简述")
    triggers: list[str] = Field(default_factory=list, description="触发提示词")
    key: str = Field(..., description="桶内完整 key,如 sops/travel.md")
    updated_at: int | None = Field(default=None, description="最后修改时间(Unix 秒),未知为 null")


class SopDetail(SopSummary):
    """SOP 详情(在 Summary 基础上带 Markdown 正文,供详情/编辑页)。"""

    body: str = Field(default="", description="Markdown 正文")


class SopWrite(BaseModel):
    """新建 / 更新 SOP 的请求体(id 不可改:新建时定名,更新须与路径一致)。"""

    id: str = Field(..., description="SOP 唯一标识(字母/数字/下划线/连字符,字母或数字开头)")
    name: str = Field(..., min_length=1, description="人类可读名称")
    description: str = Field(default="", description="用途简述")
    triggers: list[str] = Field(default_factory=list, description="触发提示词")
    body: str = Field(default="", description="Markdown 正文")
