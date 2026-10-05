"""认证接口:注册 / 验证邮箱 / 登录 / 刷新 / 登出 / 我的账号 / 设备管理。

三条约定(与 docs/认证鉴权与用户管理技术方案.md §5 对应):
1. **令牌出口分离**:access token 走响应体(前端只放内存),refresh token **只**走
   HttpOnly cookie —— JS 读不到,也就不会被 XSS 顺手带走;
2. **CSRF**:凡是「靠 cookie 认证」的接口(/refresh)必须带 X-CSRF-Token,与 qa_csrf
   cookie 做双提交比对;靠 Authorization 头认证的接口天然不受 CSRF 影响(攻击者
   没法让浏览器自动带上别人的 Bearer 头),就不强求;
3. **限流失败关闭**:注册 / 登录 / 找回按「IP + 邮箱摘要」双重计数,Redis 不可用时
   由 ratelimit 抛 503 —— 不放开限制(见 app/auth/ratelimit.py)。

错误一律抛认证异常,由 app/auth/http.py 统一映射成 {"detail": {"code", "message"}},
本文件不吞异常。参数校验(邮箱格式 / 密码策略)由 service 负责,失败也是同一套错误体。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.auth import ratelimit, tokens
from app.auth import service as auth_service
from app.auth.deps import (
    auth_ready, client_ip, get_auth, public_endpoint, require_user, user_agent,
)
from app.auth.errors import InvalidToken
from app.auth.service import Principal
from app.config import get_settings
from app.schemas.auth import (
    LoginRequest, MessageOut, PasswordChangeRequest, PasswordResetRequest, RegisterRequest,
    ResendRequest, SessionList, SessionOut, TokenOut, UserOut, VerifyEmailRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix=get_settings().api_prefix + "/auth",
    tags=["auth"],
    # 每条认证路由都先过这道关:数据库 / 签名密钥没配好 → 503 且写明缺什么。
    # 挂在 router 上而不是逐个函数写:新增接口不会漏(结构用例另有断言看守)。
    dependencies=[Depends(auth_ready)],
)

CSRF_COOKIE_NAME = "qa_csrf"


# ---- 请求级小工具 ----

def _key(value: str) -> str:
    """限流计数键:邮箱等敏感串一律用摘要 —— Redis 里不出现明文地址。"""
    return tokens.sha256_hex(value)[:16]


def _cookie_kwargs() -> dict:
    s = get_settings()
    kwargs = {
        "path": router.prefix,          # 只发给 /api/v1/auth/*:别的接口拿不到这个 cookie
        "samesite": "lax",
        "secure": bool(s.auth_cookie_secure),
    }
    if s.auth_cookie_domain:
        kwargs["domain"] = s.auth_cookie_domain
    return kwargs


def _max_age_until(expires_at: datetime | None) -> int:
    """cookie 存活秒数(不超过会话本身的剩余时间)。"""
    if expires_at is None:
        return max(1, int(get_settings().auth_refresh_token_days)) * 86400
    left = (expires_at - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
    return max(0, int(left))


def _set_session_cookies(response: Response, *, refresh_token: str, refresh_expires_at,
                         csrf_token: str) -> None:
    """登录 / 刷新成功后落两个 cookie:refresh(HttpOnly)+ csrf(非 HttpOnly,给 JS 读)。"""
    kwargs = _cookie_kwargs()
    max_age = _max_age_until(refresh_expires_at)
    response.set_cookie(get_settings().auth_cookie_name, refresh_token,
                        max_age=max_age, httponly=True, **kwargs)
    # 双提交 CSRF:同值写一份非 HttpOnly 的 cookie,前端读出来放进 X-CSRF-Token 头。
    # 它本身不是秘密(cookie 与头都来自同源 JS),安全性来自「别的站点读不到、也设不了」。
    response.set_cookie(CSRF_COOKIE_NAME, csrf_token, max_age=max_age, httponly=False, **kwargs)


def _clear_session_cookies(response: Response) -> None:
    kwargs = _cookie_kwargs()
    response.delete_cookie(get_settings().auth_cookie_name, **kwargs)
    response.delete_cookie(CSRF_COOKIE_NAME, **kwargs)


def _require_csrf(request: Request) -> None:
    """cookie 认证接口的 CSRF 校验:头里的令牌必须与 cookie 一致(常量时间比较)。"""
    cookie = request.cookies.get(CSRF_COOKIE_NAME) or ""
    header = request.headers.get("x-csrf-token") or ""
    if not cookie or not header or not tokens.constant_time_equal(cookie, header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            # 失配 = 疑似 CSRF;整枚缺失 = 前端 cookie 被清或被浏览器策略拦了,前端可
            # 重新取一枚(/auth/csrf)再试一次 —— 所以两种情况分开编码。
            detail={"code": "csrf_mismatch" if cookie else "csrf_missing",
                    "message": "请求校验失败,请刷新页面后重试"},
        )


def _token_out(issued: auth_service.IssuedTokens) -> TokenOut:
    return TokenOut(
        access_token=issued.access_token,
        access_expires_at=issued.access_expires_at.isoformat(),
        refresh_expires_at=issued.refresh_expires_at.isoformat(),
        user=UserOut(**issued.user),
    )


# ---- 注册 / 邮箱验证 ----

@router.post("/register", response_model=MessageOut, dependencies=[Depends(public_endpoint)])
async def register(payload: RegisterRequest, request: Request) -> MessageOut:
    """注册。成功与「邮箱已存在」返回同一句中性文案(不透露账号是否存在)。

    状态机是 pending_email,验证邮件发出后等本人点链接;管理员审批是后面独立的一步。
    """
    from app.auth.emails import normalize_email

    ip = client_ip(request)
    await ratelimit.hit(f"register:ip:{ip}", limit=10, window_seconds=300)
    norm = normalize_email(payload.email)          # 格式问题当场报 400(与账号存在性无关)
    await ratelimit.hit(f"register:email:{_key(norm)}", limit=3, window_seconds=3600)
    service = get_auth(request)
    message = await service.register(email=norm, password=payload.password,
                                     display_name=payload.display_name, ip=ip)
    return MessageOut(message=message)


@router.post("/verify-email", response_model=MessageOut, dependencies=[Depends(public_endpoint)])
async def verify_email(payload: VerifyEmailRequest, request: Request) -> MessageOut:
    """消费验证邮件里的令牌 → 账号进入待审批。

    刻意只做 POST:邮件里的链接指向**前端页面**,由页面读 query 里的令牌再调这个接口。
    这样哪怕邮件被安全网关 / 爬虫 GET 预取,也不会顺手把验证给「用掉」(GET 不执行敏感操作)。
    """
    ip = client_ip(request)
    await ratelimit.hit(f"verify:ip:{ip}", limit=30, window_seconds=300)
    service = get_auth(request)
    result = await service.verify_email(token=payload.token, ip=ip)
    return MessageOut(message=result["message"])


@router.post("/verify-email/resend", response_model=MessageOut, dependencies=[Depends(public_endpoint)])
async def resend_verification(payload: ResendRequest, request: Request) -> MessageOut:
    """重发验证邮件。中性回复;同一邮箱 60 秒内只发一封(超出静默忽略)。"""
    ip = client_ip(request)
    await ratelimit.hit(f"resend:ip:{ip}", limit=10, window_seconds=300)
    service = get_auth(request)
    await service.resend_verification(email=payload.email, ip=ip)
    return MessageOut(message="如果该邮箱还在等待验证,我们已经重新发送了一封验证邮件。")


# ---- 登录 / 刷新 / 登出 ----

@router.post("/login", response_model=TokenOut, dependencies=[Depends(public_endpoint)])
async def login(payload: LoginRequest, request: Request, response: Response) -> TokenOut:
    """登录:账密正确且账号 active 才发凭证。

    状态不对(待验证 / 待审批 / 被拒 / 停用)只在**密码验证通过之后**才提示,
    否则这个接口就成了账号状态探测器。
    """
    ip = client_ip(request)
    await ratelimit.hit(f"login:ip:{ip}", limit=30, window_seconds=300)
    await ratelimit.hit(f"login:email:{_key(payload.email.strip().lower())}",
                        limit=10, window_seconds=300)
    service = get_auth(request)
    issued = await service.login(email=payload.email, password=payload.password, ip=ip,
                                 user_agent=user_agent(request))
    _set_session_cookies(response, refresh_token=issued.refresh_token,
                         refresh_expires_at=issued.refresh_expires_at,
                         csrf_token=tokens.new_random_token()[0])
    return _token_out(issued)


@router.post("/refresh", response_model=TokenOut, dependencies=[Depends(public_endpoint)])
async def refresh(request: Request, response: Response) -> TokenOut:
    """用 refresh cookie 换一枚新的 access token(顺带轮换 refresh token)。

    * 这条接口靠 cookie 认证,所以必须过 CSRF 双提交;
    * 令牌轮换:旧的立刻作废。宽限窗口内的重复提交(前端并发 / 网络重试)照常放行,
      超出窗口再用旧令牌则判定为重放 —— 撤销整个会话(app/auth/service.py:refresh)。
    """
    _require_csrf(request)
    ip = client_ip(request)
    await ratelimit.hit(f"refresh:ip:{ip}", limit=60, window_seconds=300)
    raw = request.cookies.get(get_settings().auth_cookie_name) or ""
    if not raw:
        raise InvalidToken("没有可用的登录会话,请重新登录")
    service = get_auth(request)
    issued = await service.refresh(refresh_token=raw, ip=ip)
    _set_session_cookies(response, refresh_token=issued.refresh_token,
                         refresh_expires_at=issued.refresh_expires_at,
                         csrf_token=tokens.new_random_token()[0])
    return _token_out(issued)


@router.get("/csrf", dependencies=[Depends(public_endpoint)])
async def issue_csrf(request: Request, response: Response) -> dict:
    """取一枚 CSRF cookie(浏览器里 csrf cookie 丢了、refresh cookie 还在时的自愈入口)。

    不涉及任何账号数据,也不需要认证 —— 它只是发一枚随机串。
    """
    await ratelimit.hit(f"csrf:ip:{client_ip(request)}", limit=60, window_seconds=300)
    raw = tokens.new_random_token()[0]
    response.set_cookie(CSRF_COOKIE_NAME, raw, max_age=max(1, int(get_settings().auth_refresh_token_days)) * 86400,
                        httponly=False, **_cookie_kwargs())
    return {"csrf_token": raw}


@router.post("/logout", response_model=MessageOut)
async def logout(request: Request, response: Response,
                 principal: Principal = Depends(require_user)) -> MessageOut:
    """登出当前设备:撤销当前会话(连带其刷新令牌)。

    靠 Bearer 认证(不靠 cookie):Bearer 不是「浏览器自动带上的凭据」,不存在 CSRF 问题;
    过期的 access token 由前端统一封装先刷新再重试,刷新失败说明会话本就不可用。
    """
    service = get_auth(request)
    raw = request.cookies.get(get_settings().auth_cookie_name) or ""
    await service.logout(session_id=principal.session_id, user_id=principal.user_id,
                         ip=client_ip(request), refresh_token=raw)
    _clear_session_cookies(response)
    return MessageOut(message="已退出登录。")


@router.post("/logout-all", response_model=MessageOut)
async def logout_all(request: Request, response: Response,
                     principal: Principal = Depends(require_user)) -> MessageOut:
    """退出全部设备:撤销该用户所有会话并递增 auth_version(在途 access token 立即失效)。"""
    service = get_auth(request)
    revoked = await service.logout_all(user_id=principal.user_id, ip=client_ip(request))
    _clear_session_cookies(response)
    return MessageOut(message=f"已退出全部设备(共 {revoked} 个登录会话)。")


# ---- 我的账号 ----

@router.get("/me", response_model=UserOut)
async def me(request: Request, principal: Principal = Depends(require_user)) -> UserOut:
    """当前登录用户的信息(前端刷新后用它判断登录状态 —— 而不是靠本地存的过期时间)。"""
    service = get_auth(request)
    profile = await service.get_profile(user_id=principal.user_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "unauthorized", "message": "账号不存在"})
    return UserOut(**profile)


@router.post("/password/change", response_model=MessageOut)
async def change_password(payload: PasswordChangeRequest, request: Request, response: Response,
                          principal: Principal = Depends(require_user)) -> MessageOut:
    """改密:验证当前密码 → 换哈希 + auth_version+=1 + 撤销全部会话(含本次)。

    撤销全部是刻意的:密码变了,所有旧会话都不该继续有效。响应里清掉 cookie,
    前端据此清空状态回登录页。
    """
    ip = client_ip(request)
    await ratelimit.hit(f"pwdchange:ip:{ip}", limit=10, window_seconds=300)
    service = get_auth(request)
    await service.change_password(user_id=principal.user_id,
                                  current_password=payload.current_password,
                                  new_password=payload.new_password, ip=ip)
    _clear_session_cookies(response)
    return MessageOut(message="密码已更新。所有设备都已退出登录,请用新密码重新登录。")


@router.post("/password/forgot", response_model=MessageOut, dependencies=[Depends(public_endpoint)])
async def forgot_password(payload: ResendRequest, request: Request) -> MessageOut:
    """找回密码:发送重置邮件。中性回复(邮箱不存在也照样说「已发送」)。"""
    ip = client_ip(request)
    await ratelimit.hit(f"forgot:ip:{ip}", limit=10, window_seconds=300)
    await ratelimit.hit(f"forgot:email:{_key(payload.email.strip().lower())}",
                        limit=3, window_seconds=3600)
    service = get_auth(request)
    await service.forgot_password(email=payload.email, ip=ip)
    return MessageOut(message="如果该邮箱对应一个账号,我们已经发送了一封重置密码邮件。")


@router.post("/password/reset", response_model=MessageOut, dependencies=[Depends(public_endpoint)])
async def reset_password(payload: PasswordResetRequest, request: Request) -> MessageOut:
    """重置密码:消费一次性令牌 → 换密码 + 撤销全部会话。令牌单次使用、30 分钟过期。"""
    ip = client_ip(request)
    await ratelimit.hit(f"reset:ip:{ip}", limit=10, window_seconds=300)
    service = get_auth(request)
    result = await service.reset_password(token=payload.token,
                                          new_password=payload.new_password, ip=ip)
    return MessageOut(message=result["message"])


# ---- 设备(会话)管理 ----

@router.get("/sessions", response_model=SessionList)
async def list_sessions(request: Request, principal: Principal = Depends(require_user)) -> SessionList:
    """我当前所有有效的登录会话(只会看到自己的 —— 查询按 user_id 过滤)。"""
    service = get_auth(request)
    rows = await service.list_sessions(user_id=principal.user_id)
    items = [SessionOut(**row, current=(row["id"] == principal.session_id)) for row in rows]
    return SessionList(items=items)


@router.delete("/sessions/{session_id}", response_model=MessageOut)
async def revoke_session(session_id: str, request: Request, response: Response,
                         principal: Principal = Depends(require_user)) -> MessageOut:
    """下线某个设备。只能撤自己的会话;不是自己的会话当作不存在(404),不泄露存在性。"""
    service = get_auth(request)
    ok = await service.revoke_session(user_id=principal.user_id, session_id=session_id,
                                      ip=client_ip(request))
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "session_not_found", "message": "该登录会话不存在"})
    if session_id == principal.session_id:
        _clear_session_cookies(response)          # 撤的是自己这台:顺手清 cookie
    return MessageOut(message="该设备已退出登录。")
