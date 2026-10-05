"""认证异常 → HTTP 响应的唯一映射处(挂成 FastAPI 异常处理器)。

为什么集中在这里而不是每个路由 try/except:
- 漏掉一处就是一处 500(甚至更糟:被宽泛的 except 吞成 200),集中映射则一次覆盖所有接口;
- 错误体形状统一成 {"detail": {"code", "message"}}:code 给前端做分流
  (如 account_pending_approval → 跳「等待审批」页),message 直接展示给用户;
- 状态码语义固定:401 凭证不行 / 403 身份不行 / 404 目标不存在 / 409 状态不允许 /
  429 限流(带 Retry-After)/ 503 部署缺配置或依赖不可用。

日志级别也在这里定:401 类只记 info(攻击者可刷,不能拿来灌磁盘),503 类记
warning/error(部署问题,要显眼)。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.auth.errors import (
    AccountNotUsable, AuthNotConfigured, InvalidCredentials, InvalidInput, InvalidToken,
    InvalidUserState, MailNotConfigured, MailSendFailed, RateLimited, RateLimitUnavailable,
    TokenReplayed, UserNotFound,
)

logger = logging.getLogger(__name__)

# 异常类型 → (状态码, 错误码)。顺序不重要,查表按类型精确匹配。
STATUS_MAP: list[tuple[type, int, str]] = [
    (InvalidInput, 400, "invalid_input"),           # 子类各自覆写 code,见 _payload
    (InvalidCredentials, 401, "invalid_credentials"),
    (TokenReplayed, 401, "token_replayed"),
    (InvalidToken, 401, "invalid_token"),
    (AccountNotUsable, 403, "account_not_usable"),
    (UserNotFound, 404, "user_not_found"),
    (InvalidUserState, 409, "invalid_user_state"),
    (RateLimited, 429, "rate_limited"),
    (RateLimitUnavailable, 503, "rate_limit_unavailable"),
    (MailNotConfigured, 503, "mail_not_configured"),
    (MailSendFailed, 503, "mail_send_failed"),
    (AuthNotConfigured, 503, "auth_not_configured"),
]

_SERVER_SIDE = (AuthNotConfigured, MailNotConfigured, MailSendFailed, RateLimitUnavailable)


def _payload(exc: BaseException, code: str) -> dict:
    detail: dict[str, object] = {"code": getattr(exc, "code", code), "message": str(exc)}
    if isinstance(exc, AccountNotUsable):
        # 账号状态单独给一个码,前端据此跳不同页面(等待审批 / 被拒 / 被停用)。
        detail["code"] = f"account_{exc.status}"
        detail["status"] = exc.status
    return {"detail": detail}


def error_response(exc: BaseException) -> tuple[JSONResponse, int] | None:
    """把异常翻成 (响应, 日志级别)。不是认证异常则返回 None(交给 FastAPI 默认处理)。"""
    for exc_type, status_code, code in STATUS_MAP:
        if not isinstance(exc, exc_type):
            continue
        headers = {}
        if status_code == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if isinstance(exc, RateLimited):
            headers["Retry-After"] = str(max(1, int(exc.retry_after)))
        # 401/403/404:可预期,记 info;429:值得留意,记 warning;503:部署问题,记 error。
        level = logging.ERROR if isinstance(exc, _SERVER_SIDE) else (
            logging.WARNING if status_code == 429 else logging.INFO)
        return JSONResponse(status_code=status_code, content=_payload(exc, code), headers=headers), level
    return None


def install(app: FastAPI) -> None:
    """把这些异常挂到应用上(create_app 里调用一次)。"""

    async def handler(request: Request, exc: Exception) -> JSONResponse:
        resolved = error_response(exc)
        assert resolved is not None          # 只注册给了 STATUS_MAP 里的类型
        response, level = resolved
        logger.log(level, "认证异常 %s %s -> %s:%s", request.method, request.url.path,
                   response.status_code, type(exc).__name__)
        return response

    for exc_type, _, _ in STATUS_MAP:
        app.add_exception_handler(exc_type, handler)
