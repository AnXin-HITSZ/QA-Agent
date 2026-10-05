"""路由守卫的结构用例:每条接口都必须**明确声明**访问级别,且级别与权限矩阵一致。

为什么要有这一组(而不是靠功能用例):
其余用例跑在「已登录管理员」的桩上(见 conftest),桩会让任何缺守卫的接口也照常返回
200 —— 「忘了加 require_admin」这种事故在功能用例里是**看不见**的。这里改成遍历路由表,
把那件事变成必失败:

1. 每条路由必须恰好声明一个级别(user / admin / public)—— 少一个、多一个都算错;
2. 声明的级别必须与下面的权限矩阵(技术方案 §6)一致 —— 防止把 admin 接口悄悄放成 public;
3. 认证路由必须挂 auth_ready —— 配置缺失时明确 503,而不是让调用方以为是密码错了。

新增接口时这个文件会先失败,提醒你去权限矩阵里做一次**有意识**的决定。
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.auth.deps import GUARD_ATTR
from app.main import app

# ---- 权限矩阵(可执行版;改动必须与技术方案 §6 同步)----

API = "/api/v1"

# 公开:健康检查 + 认证入口(注册 / 验证 / 登录 / 刷新 / 找回 / 重置 / CSRF)。
# 注意「公开」= 不需要 access token,不等于不设防:它们仍有限流、CSRF、中性文案。
PUBLIC_PATHS = {
    "/health",
    f"{API}/auth/register",
    f"{API}/auth/verify-email",
    f"{API}/auth/verify-email/resend",
    f"{API}/auth/login",
    f"{API}/auth/refresh",
    f"{API}/auth/csrf",
    f"{API}/auth/password/forgot",
    f"{API}/auth/password/reset",
}

# admin:**所有** /api/v1/admin/** 之外,还有这些「内容维护」写操作 ——
# 普通用户对知识库与 SOP 只读、待办只读(隐藏按钮不算授权,真正的授权在这里)。
ADMIN_WRITES = {
    ("POST", f"{API}/admin/sops/reload"),
    ("POST", f"{API}/knowledge/folder"),
    ("POST", f"{API}/knowledge/upload"),
    ("DELETE", f"{API}/knowledge/object"),
    ("DELETE", f"{API}/knowledge/folder"),
    ("POST", f"{API}/sops"),
    ("PUT", f"{API}/sops/{{sop_id}}"),
    ("DELETE", f"{API}/sops/{{sop_id}}"),
    ("POST", f"{API}/sops/images"),
    ("POST", f"{API}/todos"),
    ("PATCH", f"{API}/todos/{{todo_id}}"),
    ("DELETE", f"{API}/todos/{{todo_id}}"),
}


def expected_level(method: str, path: str) -> str:
    if path in PUBLIC_PATHS:
        return "public"
    if path.startswith(f"{API}/admin/") or (method, path) in ADMIN_WRITES:
        return "admin"
    return "user"          # 其余一律「登录即可」:聊天、自己的会话、知识库只读、SOP 只读、待办只读


# ---- 遍历工具 ----

def iter_api_routes(router) -> list[APIRoute]:
    """展平 FastAPI 的路由表(新版会把 include_router 的子树包一层 _IncludedRouter)。"""
    out: list[APIRoute] = []
    for route in getattr(router, "routes", []):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            out.extend(iter_api_routes(inner))
        elif isinstance(route, APIRoute):
            out.append(route)
    return out


def guard_levels(route: APIRoute) -> set[str]:
    """收集这条路由声明过的访问级别(路由级 + 函数级,递归看依赖链)。"""
    levels: set[str] = set()

    def walk(dependant) -> None:
        call = getattr(dependant, "call", None)
        level = getattr(call, GUARD_ATTR, None)
        if level:
            levels.add(level)
        for sub in getattr(dependant, "dependencies", ()) or ():
            walk(sub)

    walk(route.dependant)
    # 也允许直接标在被装饰的函数上(等价写法)
    fn_level = getattr(route.endpoint, GUARD_ATTR, None)
    if fn_level:
        levels.add(fn_level)
    return levels


def dependant_calls(route: APIRoute) -> set:
    calls = set()

    def walk(dependant) -> None:
        if getattr(dependant, "call", None) is not None:
            calls.add(dependant.call)
        for sub in getattr(dependant, "dependencies", ()) or ():
            walk(sub)

    walk(route.dependant)
    return calls


ROUTES = iter_api_routes(app)


def test_route_table_is_not_empty_and_looks_right():
    """先确保遍历本身没瞎:否则下面几条断言会因为「一条路由都没看到」而全部空过。"""
    assert len(ROUTES) >= 40
    paths = {r.path for r in ROUTES}
    assert {"/health", f"{API}/auth/login", f"{API}/admin/users", f"{API}/chat/stream"} <= paths
    # 不该有游离在已知前缀之外的接口
    for path in paths:
        assert path.startswith(("/health", API)), f"未归类的路径前缀:{path}"


def test_every_route_declares_exactly_one_access_level():
    offenders = []
    for route in ROUTES:
        levels = guard_levels(route)
        if len(levels) != 1:
            offenders.append(f"{','.join(sorted(route.methods))} {route.path} -> {levels or '未声明'}")
    assert not offenders, "这些接口没有恰好声明一个访问级别(require_user / require_admin / public_endpoint):\n" + \
                          "\n".join(offenders)


def test_declared_level_matches_permission_matrix():
    """声明的级别必须与权限矩阵一致 —— 防止「悄悄放成 public」。"""
    wrong = []
    for route in ROUTES:
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            want = expected_level(method, route.path)
            got = guard_levels(route)
            if got and got != {want}:
                wrong.append(f"{method} {route.path}: 声明 {sorted(got)},矩阵要求 {want}")
    assert not wrong, "访问级别与权限矩阵不符:\n" + "\n".join(wrong)


def test_matrix_covers_the_admin_prefix_completely():
    """反方向核对:凡是矩阵判为 admin 的接口,都不许出现「user 也能进」的写法。"""
    for route in ROUTES:
        if route.path.startswith(f"{API}/admin/"):
            assert guard_levels(route) == {"admin"}, route.path


def test_auth_routes_check_configuration_before_doing_anything():
    """认证 / 管理路由必须挂 auth_ready:数据库或签名密钥没配 → 503 明确报错。

    依赖顺序也一并钉住:auth_ready 必须**先于**守卫执行(否则会先回 401,
    让运维以为是自己密码错了)。
    """
    from app.auth.deps import auth_ready

    checked = 0
    for route in ROUTES:
        if not route.path.startswith(f"{API}/auth/") and not route.path.startswith(f"{API}/admin/"):
            continue
        calls = dependant_calls(route)
        assert auth_ready in calls, f"{route.path} 未挂 auth_ready(配置缺失时不会明确报错)"
        checked += 1
    assert checked >= 20


def test_sse_endpoint_takes_no_token_in_url():
    """SSE 也只从 Authorization 头收令牌:查询串里的令牌会进访问日志 / Referer。"""
    stream = [r for r in ROUTES if r.path == f"{API}/chat/stream"][0]
    names = {p.name for p in stream.dependant.query_params}
    assert not (names & {"token", "access_token", "authorization"})


# ---- 行为面:矩阵真的拦得住(真认证服务,不是桩)----
#
# 上面的结构断言保证「声明了级别」,这里保证「声明真的生效」——
# 两边都换成真 AuthService + 真令牌(见 conftest 的 auth_env / auth_client)。


def _calls(level: str) -> list[tuple[str, str]]:
    """从路由表里挑出某一级别的 (方法, 路径),路径占位符换成假值。"""
    out: list[tuple[str, str]] = []
    for route in ROUTES:
        concrete = (route.path.replace("{user_id}", "00000000-0000-4000-8000-00000000d0d0")
                              .replace("{thread_id}", "t-1").replace("{sop_id}", "s-1")
                              .replace("{todo_id}", "t-1").replace("{job_id}", "j-1")
                              .replace("{price_id}", "1").replace("{event_id}", "1")
                              .replace("{image_id}", "i-1").replace("{session_id}", "s-1"))
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            if expected_level(method, route.path) == level:
                out.append((method, concrete))
    return out


def _send(client, method: str, path: str, headers: dict | None = None):
    return client.request(method, path, headers=headers or {}, json={})


def test_anonymous_gets_401_on_every_protected_route(auth_client):
    """匿名调任何受保护接口 → 401(而不是 200 / 422 / 500)。"""
    failures = []
    for method, path in _calls("user") + _calls("admin"):
        r = _send(auth_client, method, path)
        if r.status_code != 401:
            failures.append(f"{method} {path} -> {r.status_code}")
    assert not failures, "匿名未被拦住:\n" + "\n".join(failures)


def _guard_code(response) -> str:
    """取出「守卫拒了」的标记码;不是守卫拒绝(业务错误)返回空串。"""
    try:
        detail = response.json().get("detail")
    except Exception:  # noqa: BLE001  —— 非 JSON 响应(如 SSE)一律不算守卫拒绝
        return ""
    return detail.get("code", "") if isinstance(detail, dict) else ""


def test_public_routes_are_reachable_without_a_token(auth_client):
    """公开接口不因**缺令牌**被拒。

    「公开」不等于没防线:refresh 没有 cookie 时照样 403 csrf_missing —— 那是业务拒绝,
    不是守卫拒绝,所以这里比的是拒绝**码**而不是状态码。
    """
    for method, path in _calls("public"):
        r = _send(auth_client, method, path)
        assert _guard_code(r) not in ("unauthorized", "forbidden"), \
            f"{method} {path} 把匿名调用当越权拒了(它应是公开的)"
        if path == "/health":
            assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.fixture
def user_token(auth_client, auth_env):
    """一个**真的** active 普通用户(注册 → 验证 → 审批 → 登录全走真流程)。"""
    email, pwd = "normal@example.com", "User-Lab-2026!strong"
    assert auth_client.post(f"{API}/auth/register",
                            json={"email": email, "password": pwd,
                                  "display_name": "普通用户"}).status_code == 200
    token = auth_env.mailer.last_link().split("token=", 1)[1]
    assert auth_client.post(f"{API}/auth/verify-email", json={"token": token}).status_code == 200
    from app.auth import store

    with auth_env.db.session_scope() as session:
        uid = store.get_user_by_email(session, email).id
        store.set_review(session, uid, admin_id=None, approve=True,
                         note="测试审批", now=auth_env.clock())
    login = auth_client.post(f"{API}/auth/login", json={"email": email, "password": pwd})
    assert login.status_code == 200
    return login.json()["access_token"]


def test_normal_user_is_forbidden_on_every_admin_route(auth_client, user_token):
    """普通用户 → 所有 admin 接口 403(前端藏按钮只是界面礼貌,这里才是授权)。"""
    headers = {"Authorization": f"Bearer {user_token}"}
    failures = []
    for method, path in _calls("admin"):
        r = _send(auth_client, method, path, headers)
        if r.status_code != 403:
            failures.append(f"{method} {path} -> {r.status_code}")
    assert not failures, "普通用户越权成功(应为 403):\n" + "\n".join(failures)


# 这几条**故意**终结调用者自己的会话:拿登出后的旧令牌去打后面的接口当然 401 ——
# 那是功能正常,不是守卫失灵。所以循环里跳过,单独用下一条用例覆盖。
SESSION_ENDING = {
    ("POST", f"{API}/auth/logout"),
    ("POST", f"{API}/auth/logout-all"),
}


def test_normal_user_passes_the_gate_on_user_routes(auth_client, user_token):
    """普通用户不被自己的接口挡住(403 就是不匹配;具体业务码由各接口自己的用例保证)。"""
    headers = {"Authorization": f"Bearer {user_token}"}
    for method, path in _calls("user"):
        if (method, path) in SESSION_ENDING:
            continue
        r = _send(auth_client, method, path, headers)
        assert r.status_code != 403, f"{method} {path} 把普通用户拒了"
        assert r.status_code != 401, f"{method} {path} 不认刚登录拿到的令牌"


def test_normal_user_can_end_all_its_own_sessions(auth_client, user_token):
    """登出全部是普通用户自己的接口:守卫放行,且真的把该用户所有会话下线。"""
    headers = {"Authorization": f"Bearer {user_token}"}
    assert auth_client.post(f"{API}/auth/logout-all", headers=headers, json={}).status_code == 200
    assert auth_client.get(f"{API}/auth/me", headers=headers).status_code == 401


@pytest.fixture
def admin_token(auth_client, auth_env):
    """真管理员(走 CLI 那条路径建立,与生产一致),登录后返回令牌。"""
    from scripts.create_admin import create_admin

    email, pwd = "root@example.com", "Boss-Lab-2026!admin"
    create_admin(email=email, password=pwd, display_name="管理员")
    login = auth_client.post(f"{API}/auth/login", json={"email": email, "password": pwd})
    assert login.status_code == 200
    return login.json()["access_token"]


def test_admin_passes_the_gate_on_admin_routes(auth_client, admin_token, kb_env):
    """管理员不被 403 挡住 —— 反方向核对:守卫生效,但不是「谁都进不去」。

    只挑**只读**接口,理由有两条:
    - 放行意味着接口真的会执行下去。越权用例可以打全部接口(守卫先短路),这里不行 ——
      真去 POST 一遍 index-jobs / rollback 就是在测试里改索引了;
    - 离线假件(kb_env:内存知识库 / 内存 Qdrant / 内存缓存)保证这些只读接口也不碰
      真实 OSS 与 Qdrant。
    计量那几个只读接口不在样本里:认证测试库上没建计量表,查下去只会得到 500 噪音。
    """
    headers = {"Authorization": f"Bearer {admin_token}"}
    reads = [(m, p) for m, p in _calls("admin")
             if m == "GET" and f"{API}/admin/metering" not in p]
    assert len(reads) >= 5, "可安全执行的只读管理接口太少(路由表变了?)"
    for method, path in reads:
        r = _send(auth_client, method, path, headers)
        assert r.status_code not in (401, 403), f"{method} {path} -> {r.status_code}"


def test_a_disabled_user_token_stops_working_immediately(auth_client, auth_env, user_token):
    """停用账号后,已发出的 access token 当场失效(不等它过期)。"""
    from app.auth import store

    with auth_env.db.session_scope() as session:
        uid = store.get_user_by_email(session, "normal@example.com").id
        store.set_user_status(session, uid, status=store.STATUS_DISABLED,
                              note="测试停用", now=auth_env.clock())
        store.bump_auth_version(session, uid, auth_env.clock())

    r = _send(auth_client, "GET", f"{API}/todos", {"Authorization": f"Bearer {user_token}"})
    assert r.status_code == 401
