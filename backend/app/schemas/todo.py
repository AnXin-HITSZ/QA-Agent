"""待办清单接口的请求 / 响应模型。

全局一份:所有人看到同一份待办;读对所有登录用户开放,写(增 / 改 / 删)仅管理员 ——
权限由路由上的守卫决定(见 routes/todos.py),这里的模型只管数据形状。

Agent 只读——每轮对话前后端把「未完成待办」渲染进 system prompt 供其参考,
但 Agent 根本不具备增删改待办的工具(见 app/graph/tools.py);增删改一律走本文件定义的
CRUD 接口,由人在前端操作。**不靠提示词约束权限**:权限是路由守卫的事。

每条待办记 who:`created_by` / `updated_by` 存的是**动作发生那一刻**的身份快照(改过显示名
的老记录不会跟着变),审计口径与 auth_audit 一致。旧数据(本字段引入前写下的)为 null。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TodoActor(BaseModel):
    """一条待办的「谁」:动作发生时的身份快照。"""

    user_id: str = Field(default="", description="操作者的用户 ID")
    display_name: str = Field(default="", description="操作者显示名(动作发生时的快照)")
    email: str = Field(default="", description="操作者邮箱(动作发生时的快照)")


class Todo(BaseModel):
    id: str = Field(..., description="待办唯一 ID(后端在创建时生成)")
    title: str = Field(..., description="待办标题 / 事项")
    category: str = Field(default="", description="分类,如「报销」「采购」;空 = 未分类(展示层渲染为「未分类」),不做强校验")
    done: bool = Field(default=False, description="是否已完成")
    created_at: str = Field(..., description="创建时间(ISO 8601,后端生成)")
    due_date: str | None = Field(default=None, description="截止日期(YYYY-MM-DD);留空表示无截止")
    created_by: TodoActor | None = Field(default=None, description="创建者;字段引入前的老数据为 null")
    updated_by: TodoActor | None = Field(default=None, description="最后一次修改者;从未改过时与创建者相同")


class TodoCreate(BaseModel):
    title: str = Field(..., min_length=1, description="待办标题 / 事项(必填)")
    category: str = Field(default="", description="分类,如「报销」「采购」;留空即未分类")
    due_date: str | None = Field(default=None, description="截止日期(YYYY-MM-DD);留空表示无截止")


class TodoUpdate(BaseModel):
    """局部更新:仅传入的字段会被改动,None / 缺省表示保持原值。"""

    title: str | None = Field(default=None, min_length=1, description="新标题;留空则不改")
    category: str | None = Field(default=None, description="新分类;null / 缺省 = 不改,空串 = 改为未分类")
    done: bool | None = Field(default=None, description="完成状态;留空则不改(勾选 / 取消勾选走这里)")
    due_date: str | None = Field(default=None, description="新截止日期(YYYY-MM-DD);留空则不改")


class TodoList(BaseModel):
    enabled: bool = Field(..., description="待办存储是否配置启用(依赖 Redis);false 表示未配置")
    degraded: bool = Field(default=False, description="已启用但本次读取失败(超时 / Redis 错误);true 时 items 恒空,前端提示重试")
    items: list[Todo] = Field(default_factory=list, description="待办列表:未完成在前,再按截止日期升序")
