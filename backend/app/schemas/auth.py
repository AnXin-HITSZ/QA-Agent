"""认证 / 用户管理接口的请求与响应模型。

约定(与后端实现互为表里,改动要成对):
- **refresh token 绝不进 JSON 响应体**:它只走 HttpOnly cookie(app/api/routes/auth.py
  设置),前端 JS 拿不到,也就不会被 XSS 顺手偷走;
- **access token 只回给前端内存**:响应里有,但不该被写进 localStorage / URL(前端
  api 封装里落实);
- 用户对象只暴露白名单字段:password_hash / 令牌摘要这类东西不进任何响应模型;
- 错误不用这些模型:统一是 FastAPI 的 {"detail": {"code", "message"}},前端按 code 分流
  (见前端 src/stores/auth.ts)。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ---- 用户 ----


class UserOut(BaseModel):
    """对外用户信息(与 app.auth.service.public_user 的字段一一对应)。"""

    id: str = Field(..., description="用户 ID(UUID)")
    email: str = Field(..., description="规范化后的邮箱(整体小写)")
    display_name: str = Field(..., description="显示名")
    role: str = Field(..., description="角色:user / admin")
    status: str = Field(..., description="状态:pending_email / pending_approval / active / rejected / disabled")
    email_verified: bool = Field(..., description="邮箱是否已验证(与是否被批准是两件事)")
    created_at: str | None = Field(default=None, description="注册时间(ISO 8601,UTC)")
    last_login_at: str | None = Field(default=None, description="最近一次登录时间;从未登录为 null")


class AdminUserOut(UserOut):
    """管理员视图:多出审批痕迹。"""

    reviewed_at: str | None = Field(default=None, description="审批 / 禁用时间")
    reviewed_by: str | None = Field(default=None, description="审批人(管理员)的 user_id")
    review_note: str = Field(default="", description="审批备注 / 停用原因")


# ---- 注册 / 邮箱验证 ----


class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254, description="邮箱(大小写不敏感;注册后按原样小写保存)")
    password: str = Field(..., min_length=1, description="密码(策略由后端校验:长度 / 字符类别 / 常见弱口令)")
    display_name: str = Field(default="", max_length=64, description="显示名;留空则取邮箱 @ 前的部分")


class VerifyEmailRequest(BaseModel):
    token: str = Field(..., min_length=8, description="邮件链接里的一次性令牌")


class ResendRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254, description="接收验证邮件的邮箱")


class MessageOut(BaseModel):
    """中性文案:注册 / 重发 / 找回密码都返回同一种形状,不透露账号是否存在。"""

    message: str = Field(..., description="给用户看的提示语")


# ---- 登录 / 刷新 / 登出 ----


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1)


class TokenOut(BaseModel):
    """登录 / 刷新成功的响应体:access token 在 body,refresh token 在 HttpOnly cookie。"""

    access_token: str = Field(..., description="访问令牌(JWT);前端只放在内存里")
    token_type: str = Field(default="bearer", description="固定 bearer")
    access_expires_at: str = Field(..., description="访问令牌到期时间(ISO 8601,UTC)")
    refresh_expires_at: str = Field(..., description="本次登录的会话到期时间(ISO 8601,UTC);到点需重新登录")
    user: UserOut = Field(..., description="当前用户")


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=1, description="当前密码")
    new_password: str = Field(..., min_length=1, description="新密码(不得与当前密码相同)")


class PasswordResetRequest(BaseModel):
    token: str = Field(..., min_length=8, description="重置邮件里的一次性令牌")
    new_password: str = Field(..., min_length=1)


# ---- 会话(设备)管理 ----


class SessionOut(BaseModel):
    id: str = Field(..., description="会话 ID(即 access token 里的 sid)")
    user_agent: str = Field(default="", description="登录时的客户端 UA(截断保存)")
    ip: str = Field(default="", description="登录时的来源 IP")
    created_at: str | None = Field(default=None, description="登录时间")
    last_used_at: str | None = Field(default=None, description="最近使用时间")
    expires_at: str | None = Field(default=None, description="该会话到期时间")
    current: bool = Field(default=False, description="是否是当前这次请求所用的会话")


class SessionList(BaseModel):
    items: list[SessionOut] = Field(default_factory=list, description="有效会话,最近创建在前")


# ---- 管理员:用户管理 / 审计 ----


class AdminUserList(BaseModel):
    items: list[AdminUserOut] = Field(default_factory=list)
    total: int = Field(..., description="符合筛选条件的总数(用于分页)")
    counts: dict[str, int] = Field(default_factory=dict, description="各状态人数(侧栏统计,不受当前筛选影响)")


class ReviewRequest(BaseModel):
    note: str = Field(default="", max_length=255, description="审批备注(可选;拒绝时建议写明原因)")


class RoleRequest(BaseModel):
    role: str = Field(..., description="目标角色:user / admin")


class AuditEntry(BaseModel):
    at: str = Field(..., description="发生时间(ISO 8601,UTC)")
    action: str = Field(..., description="动作:register / verify_email / login / refresh_replay / approve / disable / …")
    result: str = Field(..., description="结果:ok / denied / failed")
    actor_user_id: str | None = Field(default=None, description="操作者;系统动作为 null")
    target_user_id: str | None = Field(default=None, description="被操作用户")
    email: str = Field(default="", description="相关邮箱(便于管理员对照)")
    ip: str = Field(default="", description="来源 IP")
    note: str = Field(default="", description="备注")


class AuditList(BaseModel):
    items: list[AuditEntry] = Field(default_factory=list)
