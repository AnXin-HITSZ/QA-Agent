"""管理员接口:用户审批 / 启停 / 角色 / 审计查询。

三条不变量(技术方案 §6):
1. **每条路由都挂 require_admin**:非管理员一律 403,前端隐藏按钮只是界面礼貌,
   真正的授权在这些依赖上;
2. **管理员默认只看自己的会话**,别人的会话对他也是 404(在 conversations 路由里落实);
   用户管理看的是账号,不是内容 —— 这里不提供「读某个用户的对话」这种口子;
3. **状态流转是原子 CAS**(store.set_review / set_user_status),两个管理员同时点
   「通过 / 拒绝」不会互相覆盖;抢输的一方拿到 409 而不是静默改写。

所有动作都落 auth_audit(谁、对谁、什么时候、什么结果)。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request

from app.auth.deps import auth_ready, client_ip, get_auth, require_admin
from app.auth.service import Principal
from app.config import get_settings
from app.schemas.auth import (
    AdminUserList, AdminUserOut, AuditEntry, AuditList, ReviewRequest, RoleRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix=get_settings().api_prefix + "/admin",
    tags=["admin-users"],
    # 与认证路由同一道前置关:数据库 / 签名密钥没配好 → 503,而不是让管理员误以为密码错了。
    dependencies=[Depends(auth_ready)],
)


@router.get("/users", response_model=AdminUserList)
async def list_users(
    request: Request,
    status_: str | None = Query(default=None, alias="status",
                                description="按状态筛选:pending_email / pending_approval / active / rejected / disabled"),
    q: str = Query(default="", max_length=254, description="按邮箱或昵称模糊搜索"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_admin),
) -> AdminUserList:
    """分页列用户 + 各状态人数。MySQL 分页(不是全表捞出来在内存里切片)。"""
    service = get_auth(request)
    data = await service.list_users(status=status_, q=q, offset=offset, limit=limit)
    return AdminUserList(
        items=[AdminUserOut(**row) for row in data["items"]],
        total=data["total"],
        counts=data["counts"],
    )


@router.post("/users/{user_id}/approve", response_model=AdminUserOut)
async def approve_user(user_id: str, payload: ReviewRequest, request: Request,
                       principal: Principal = Depends(require_admin)) -> AdminUserOut:
    """审批通过 → active。只有待验证 / 待审批的账号可被审批,重复点会得到 409。"""
    service = get_auth(request)
    row = await service.review_user(admin=principal, target_id=user_id, approve=True,
                                    note=payload.note, ip=client_ip(request))
    return AdminUserOut(**row)


@router.post("/users/{user_id}/reject", response_model=AdminUserOut)
async def reject_user(user_id: str, payload: ReviewRequest, request: Request,
                      principal: Principal = Depends(require_admin)) -> AdminUserOut:
    """审批拒绝 → rejected。被拒账号无法登录,可再次由管理员改判(重新批准)。"""
    service = get_auth(request)
    row = await service.review_user(admin=principal, target_id=user_id, approve=False,
                                    note=payload.note, ip=client_ip(request))
    return AdminUserOut(**row)


@router.post("/users/{user_id}/disable", response_model=AdminUserOut)
async def disable_user(user_id: str, payload: ReviewRequest, request: Request,
                       principal: Principal = Depends(require_admin)) -> AdminUserOut:
    """停用账号:立即撤销其全部会话并递增 auth_version(进行中的令牌当场失效)。"""
    service = get_auth(request)
    row = await service.set_user_enabled(admin=principal, target_id=user_id, enable=False,
                                         note=payload.note, ip=client_ip(request))
    return AdminUserOut(**row)


@router.post("/users/{user_id}/enable", response_model=AdminUserOut)
async def enable_user(user_id: str, payload: ReviewRequest, request: Request,
                      principal: Principal = Depends(require_admin)) -> AdminUserOut:
    """恢复被停用的账号 → active(不能把被拒 / 待审批的账号直接「启用」，那种情况走审批)。"""
    service = get_auth(request)
    row = await service.set_user_enabled(admin=principal, target_id=user_id, enable=True,
                                         note=payload.note, ip=client_ip(request))
    return AdminUserOut(**row)


@router.post("/users/{user_id}/role", response_model=AdminUserOut)
async def set_role(user_id: str, payload: RoleRequest, request: Request,
                   principal: Principal = Depends(require_admin)) -> AdminUserOut:
    """改角色(user / admin)。不能改自己 —— 免得手滑把最后一个管理员降级。"""
    service = get_auth(request)
    row = await service.change_role(admin=principal, target_id=user_id, role=payload.role,
                                    ip=client_ip(request))
    return AdminUserOut(**row)


@router.get("/audit", response_model=AuditList)
async def audit_log(
    request: Request,
    target_user_id: str | None = Query(default=None, description="只看某个用户的审计"),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_admin),
) -> AuditList:
    """最近的认证审计(登录失败、重放告警、审批、停用……)。只读、倒序、有上限。"""
    service = get_auth(request)
    rows = await service.audit_log(limit=limit, target_user_id=target_user_id)
    return AuditList(items=[AuditEntry(**row) for row in rows])
