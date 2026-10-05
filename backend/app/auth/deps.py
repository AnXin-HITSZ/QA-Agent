"""FastAPI 依赖:访问令牌校验、角色判定、客户端 IP、限流封装。

三条硬规则(技术方案 §5 §8):
1. **每条路由都要声明访问级别**:require_admin / require_user / public_endpoint 三者之一。
   tests/test_auth_rbac.py 会遍历路由表逐个检查 —— 新增接口忘了加依赖会当场失败,
   而不是等到有人发现「这个接口谁都能调」。
2. **令牌只从 Authorization 头读**(SSE 也一样):绝不从 URL 查询串里收令牌
   (会进访问日志 / Referer);cookie 只承载 refresh,且只在 auth 路由里显式读取。
3. **配置缺失 = 明确报错**(503),绝不降级为匿名放行。
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status

from app.auth.errors import AuthNotConfigured, InvalidToken
from app.auth.service import AuthService, Principal
from app.auth.tokens import bearer_token, decode_access_token
from app.config import get_settings

GUARD_ATTR = "_qa_guard"        # 挂在依赖函数上的访问级别标记(结构用例读它)


def _mark(level: str):
    def wrapper(fn):
        setattr(fn, GUARD_ATTR, level)
        return fn

    return wrapper


def get_auth(request: Request) -> AuthService:
    """lifespan 注入的认证服务;没装配(如离线测试)时明确报错,不退化成放行。"""
    service = getattr(request.app.state, "auth", None)
    if service is None:
        raise AuthNotConfigured("认证服务未初始化(应用未正常启动)")
    return service


async def auth_ready(request: Request) -> AuthService:
    """认证路由的**统一前置依赖**:取服务,并先确认关键配置在位。

    检查两件事,缺任何一件都是 503 + 明确原因(绝不降级、绝不给出误导性的 401):
    - 数据库(METERING_MYSQL_URL):用户 / 会话都存那里;
    - 令牌签名密钥(AUTH_JWT_SECRET):空或过弱时 tokens.jwt_secret() 直接抛。

    写成依赖而不是散在每个函数体里:新增认证接口只要挂了它,就天然做了这套检查 ——
    反过来,漏挂会在 test_auth_rbac.py 的结构用例里现形。
    """
    from app.auth import db as auth_db
    from app.auth.tokens import jwt_secret

    if not auth_db.configured():
        raise AuthNotConfigured(
            "未配置数据库(METERING_MYSQL_URL):认证 / 用户管理不可用。"
            "见 docs/认证鉴权与用户管理技术方案.md §10。"
        )
    jwt_secret()                      # 空 / 过弱 / 仍是示例值 → AuthNotConfigured
    return get_auth(request)


def client_ip(request: Request) -> str:
    """真实客户端 IP:只在配置了可信代理层数时才看 X-Forwarded-For(从右往左数)。

    不信任 XFF 的默认姿态:客户端可以自己伪造这个头,拿它做审计 / 限流等于没做。
    """
    count = max(0, int(get_settings().auth_trusted_proxy_count))
    if count:
        parts = [p.strip() for p in (request.headers.get("x-forwarded-for") or "").split(",")
                 if p.strip()]
        if len(parts) >= count:
            return parts[-count][:45]
    return (request.client.host if request.client else "")[:45]


def user_agent(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:255]


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthorized", "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    """校验 Bearer access token:验签 → 回库核对用户 / 会话 / 版本。

    任何一步不过都统一 401(不区分「令牌过期」「会话被撤销」「用户被禁用」——
    对攻击者少给信息;真需要排查时看服务端日志与审计表)。
    """
    service = get_auth(request)
    token = bearer_token(authorization)
    if not token:
        raise _unauthorized("需要登录")
    try:
        payload = decode_access_token(token)
    except AuthNotConfigured:
        raise
    except InvalidToken as exc:
        raise _unauthorized("登录状态已失效,请重新登录") from exc

    principal = await service.load_principal(user_id=payload["sub"], session_id=payload["sid"])
    if principal is None:
        raise _unauthorized("登录状态已失效,请重新登录")
    if int(payload["ver"]) != principal.auth_version:
        # 改密 / 重置 / 登出全部设备后,旧令牌的 ver 就落伍了 —— 立即失效,不等过期。
        raise _unauthorized("登录状态已失效,请重新登录")
    return principal


@_mark("user")
async def require_user(principal: Principal = Depends(current_user)) -> Principal:
    """已登录且状态正常(active + 已验证)。"""
    return principal


@_mark("admin")
async def require_admin(principal: Principal = Depends(current_user)) -> Principal:
    """管理员:一切用户接口 + 用户管理 + 内容维护。"""
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "需要管理员权限"},
        )
    return principal


@_mark("public")
def public_endpoint() -> None:
    """显式声明「这条路由就是公开的」(健康检查、认证入口)。

    存在的意义不是鉴权,而是让「每条路由都声明过访问级别」这件事可被测试强制。
    """
    return None
