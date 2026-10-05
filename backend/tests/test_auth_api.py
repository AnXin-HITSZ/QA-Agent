"""认证接口的端到端用例:注册 → 验证 → 审批 → 登录 → 刷新 → 登出 → 改密 / 重置。

这里是**真代码**:真 AuthService、真 SQL(临时 SQLite)、真路由与依赖;只有两个替身 ——
发信(FakeMailer,绝不发真邮件)与限流计数(MemoryLimiter,绝不连 Redis)。

分文件的原因(见 conftest 的说明):其余用例用「已登录管理员」的桩跑功能,本文件与
test_auth_core.py 才是认证本身的行为断言 —— 任何一条断言都必须能被「改坏代码」杀死。
"""

from __future__ import annotations

import pytest

PWD = "Lab-QA-2026!strong"
EMAIL = "zhang@example.com"


# ---- 小工具 ----

def _register(client, email=EMAIL, password=PWD, name="张同学"):
    return client.post("/api/v1/auth/register",
                       json={"email": email, "password": password, "display_name": name})


def _verify(client, mailer):
    link = mailer.last_link()
    token = link.split("token=", 1)[1]
    return client.post("/api/v1/auth/verify-email", json={"token": token})


def _login(client, email=EMAIL, password=PWD):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _approve(auth_env, email=EMAIL):
    """把用户推到 active(直接调 store;管理员审批接口本身的用例在下面 admin_env 那组)。"""
    from app.auth import store

    with auth_env.db.session_scope() as session:
        user = store.get_user_by_email(session, email)
        store.set_review(session, user.id, admin_id=None, approve=True,
                         note="测试审批", now=auth_env.clock())
        return user.id


def _seed_active(client, auth_env, password=PWD, email=EMAIL):
    """注册 → 验证 → 审批 → 登录,返回 (登录响应, user_id)。"""
    assert _register(client, email=email, password=password).status_code == 200
    assert _verify(client, auth_env.mailer).status_code == 200
    uid = _approve(auth_env, email=email)
    return _login(client, email=email, password=password), uid


# ---- 注册 / 验证 ----

def test_register_sends_verification_mail_and_lands_pending_email(auth_client, auth_env):
    r = _register(auth_client)
    assert r.status_code == 200
    assert "验证邮件" in r.json()["message"]

    sent = auth_env.mailer.sent[-1]
    assert sent["to"] == EMAIL
    assert "token=" in sent["text"]                      # 链接里只有一次性令牌,没有密码 / access token
    assert PWD not in sent["text"]

    # 未验证之前登不了:状态机第一格是 pending_email
    r = _login(auth_client)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "account_pending_email"


def test_register_duplicate_email_is_neutral_and_does_not_resend(auth_client, auth_env):
    _register(auth_client)
    before = len(auth_env.mailer.sent)
    r = _register(auth_client)
    assert r.status_code == 200                            # 文案中性:不透露邮箱是否已注册
    assert len(auth_env.mailer.sent) == before             # 也不再发信


def test_register_rejects_bad_email_and_weak_password(auth_client):
    r = _register(auth_client, email="not-an-email")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_email"

    r = _register(auth_client, password="123456")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_password"


def test_verify_email_moves_to_pending_approval_and_token_is_single_use(auth_client, auth_env):
    _register(auth_client)
    token = auth_env.mailer.last_link().split("token=", 1)[1]

    assert auth_client.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 200
    # 同一枚令牌再点一次:不重复生效,也不再是「通过」
    r = auth_client.post("/api/v1/auth/verify-email", json={"token": token})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_token"

    # 现在状态是待审批:密码对,但还不能登录
    r = _login(auth_client)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "account_pending_approval"


def test_verify_email_rejects_forged_token(auth_client):
    r = auth_client.post("/api/v1/auth/verify-email", json={"token": "x" * 64})
    assert r.status_code == 401


# ---- 登录 ----

def test_login_before_approval_says_nothing_about_state_without_password(auth_client, auth_env):
    _register(auth_client)
    _verify(auth_client, auth_env.mailer)

    r = _login(auth_client, password="Wrong-Password-9!")
    assert r.status_code == 401                            # 密码错 → 与「账号不存在」同一句话
    assert r.json()["detail"]["code"] == "invalid_credentials"

    r = _login(auth_client, email="nobody@example.com")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_credentials"


def test_login_success_issues_access_token_and_httponly_cookie(auth_client, auth_env):
    login, uid = _seed_active(auth_client, auth_env)
    assert login.status_code == 200
    body = login.json()
    assert body["user"]["id"] == uid
    assert body["user"]["role"] == "user"
    assert "password_hash" not in str(body)

    # refresh 只在 cookie 里,且 HttpOnly;响应体里绝不出现
    raw_cookies = login.headers.get_list("set-cookie")
    refresh = [c for c in raw_cookies if c.startswith("qa_refresh=")]
    assert refresh and "HttpOnly" in refresh[0] and "SameSite=lax" in refresh[0]
    assert body["access_token"] not in login.headers.get("set-cookie", "")

    # cookie 只发给 /api/v1/auth,不该被别的接口带上
    assert "Path=/api/v1/auth" in refresh[0]

    me = auth_client.get("/api/v1/auth/me",
                         headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL


def test_me_requires_token(auth_client):
    assert auth_client.get("/api/v1/auth/me").status_code == 401
    r = auth_client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


# ---- 刷新 / 登出 ----

def _refresh(client):
    csrf = client.cookies.get("qa_csrf")
    return client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": csrf or ""})


def test_refresh_rotates_token_and_old_one_is_graced_then_replay_alerted(auth_client, auth_env):
    login, _ = _seed_active(auth_client, auth_env)
    old_cookie = auth_client.cookies.get("qa_refresh")

    r = _refresh(auth_client)
    assert r.status_code == 200
    new_cookie = auth_client.cookies.get("qa_refresh")
    assert new_cookie and new_cookie != old_cookie

    # 宽限窗口内用旧令牌再刷一次:前端并发 / 网络重试,放行
    auth_client.cookies.set("qa_refresh", old_cookie)
    assert _refresh(auth_client).status_code == 200

    # 超出窗口再用旧令牌:判定重放,整个会话被撤销
    auth_env.clock.advance(seconds=400)
    auth_client.cookies.set("qa_refresh", old_cookie)
    r = _refresh(auth_client)
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "token_replayed"

    # 撤销之后,连刚刚还有效的 access token 也失效
    assert auth_client.get("/api/v1/auth/me", headers={
        "Authorization": f"Bearer {login.json()['access_token']}"}).status_code == 401


def test_refresh_requires_csrf_token(auth_client, auth_env):
    _seed_active(auth_client, auth_env)
    assert auth_client.post("/api/v1/auth/refresh").status_code == 403
    r = auth_client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "csrf_mismatch"


def test_refresh_without_cookie_says_relogin(auth_client):
    auth_client.cookies.set("qa_csrf", "abc")
    r = auth_client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": "abc"})
    assert r.status_code == 401


def test_logout_revokes_current_session(auth_client, auth_env):
    login, _ = _seed_active(auth_client, auth_env)
    access = login.json()["access_token"]
    assert auth_client.post("/api/v1/auth/logout",
                            headers={"Authorization": f"Bearer {access}"}).status_code == 200
    assert auth_client.get("/api/v1/auth/me",
                           headers={"Authorization": f"Bearer {access}"}).status_code == 401
    assert auth_client.cookies.get("qa_refresh") is None   # cookie 已清


def test_logout_all_kills_every_session(auth_client, auth_env):
    login1, _ = _seed_active(auth_client, auth_env)
    second = _login(auth_client)                            # 第二个设备
    assert second.status_code == 200

    access1 = login1.json()["access_token"]
    assert auth_client.post("/api/v1/auth/logout-all",
                            headers={"Authorization": f"Bearer {access1}"}).status_code == 200
    # 两台设备都下线(auth_version 递增 + 全部会话撤销)
    for token in (access1, second.json()["access_token"]):
        assert auth_client.get("/api/v1/auth/me",
                               headers={"Authorization": f"Bearer {token}"}).status_code == 401


# ---- 改密 / 找回 / 重置 ----

def test_change_password_revokes_all_sessions_and_old_password_stops_working(auth_client, auth_env):
    login, _ = _seed_active(auth_client, auth_env)
    access = login.json()["access_token"]
    new_pwd = "New-Lab-QA-2026!ok"

    r = auth_client.post("/api/v1/auth/password/change",
                         headers={"Authorization": f"Bearer {access}"},
                         json={"current_password": PWD, "new_password": new_pwd})
    assert r.status_code == 200
    assert auth_client.get("/api/v1/auth/me",
                           headers={"Authorization": f"Bearer {access}"}).status_code == 401
    assert _login(auth_client, password=PWD).status_code == 401
    assert _login(auth_client, password=new_pwd).status_code == 200


def test_change_password_rejects_wrong_current_password(auth_client, auth_env):
    login, _ = _seed_active(auth_client, auth_env)
    r = auth_client.post("/api/v1/auth/password/change",
                         headers={"Authorization": f"Bearer {login.json()['access_token']}"},
                         json={"current_password": "Wrong-Password-1!", "new_password": "New-ok-2026!x"})
    assert r.status_code == 401


def test_forgot_password_is_neutral_for_unknown_email(auth_client, auth_env):
    before = len(auth_env.mailer.sent)
    r = auth_client.post("/api/v1/auth/password/forgot", json={"email": "ghost@example.com"})
    assert r.status_code == 200
    assert len(auth_env.mailer.sent) == before              # 没发信,但文案与成功时一致


def test_reset_password_flow_consumes_token_once(auth_client, auth_env):
    _seed_active(auth_client, auth_env)
    new_pwd = "Reset-Lab-2026!yes"

    assert auth_client.post("/api/v1/auth/password/forgot",
                            json={"email": EMAIL}).status_code == 200
    token = auth_env.mailer.last_link().split("token=", 1)[1]

    r = auth_client.post("/api/v1/auth/password/reset",
                         json={"token": token, "new_password": new_pwd})
    assert r.status_code == 200
    assert _login(auth_client, password=new_pwd).status_code == 200
    assert _login(auth_client, password=PWD).status_code == 401

    # 令牌一次性:再用就没了
    r = auth_client.post("/api/v1/auth/password/reset",
                         json={"token": token, "new_password": "Another-One-2026!z"})
    assert r.status_code == 401


def test_reset_password_does_not_verify_email_nor_approve(auth_client, auth_env):
    """重置密码**不**顺带验证邮箱、也不改审批状态:两件事语义不同(方案 §3)。"""
    from app.auth import store

    assert _register(auth_client).status_code == 200
    assert auth_client.post("/api/v1/auth/password/forgot",
                            json={"email": EMAIL}).status_code == 200
    token = auth_env.mailer.last_link().split("token=", 1)[1]
    assert auth_client.post("/api/v1/auth/password/reset",
                            json={"token": token,
                                  "new_password": "Reset-While-Pending-2026!a"}).status_code == 200

    with auth_env.db.session_scope() as session:
        user = store.get_user_by_email(session, EMAIL)
        assert user.status == store.STATUS_PENDING_EMAIL
        assert user.email_verified_at is None
    r = _login(auth_client, password="Reset-While-Pending-2026!a")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "account_pending_email"


# ---- 设备管理 ----

def test_sessions_list_marks_current_and_revoke_only_own(auth_client, auth_env):
    login1, uid1 = _seed_active(auth_client, auth_env)
    access1 = login1.json()["access_token"]
    second = _login(auth_client)
    access2 = second.json()["access_token"]

    r = auth_client.get("/api/v1/auth/sessions", headers={"Authorization": f"Bearer {access1}"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    current = [s for s in items if s["current"]]
    assert len(current) == 1 and current[0]["id"] == _sid(access1)

    # 撤掉另一台
    other = [s for s in items if not s["current"]][0]
    assert auth_client.delete(f"/api/v1/auth/sessions/{other['id']}",
                              headers={"Authorization": f"Bearer {access1}"}).status_code == 200
    assert auth_client.get("/api/v1/auth/me",
                           headers={"Authorization": f"Bearer {access2}"}).status_code == 401
    # 自己的还在
    assert auth_client.get("/api/v1/auth/me",
                           headers={"Authorization": f"Bearer {access1}"}).status_code == 200

    # 别人的 / 不存在的会话:404,不泄露是否存在
    r = auth_client.delete("/api/v1/auth/sessions/00000000-0000-4000-8000-0000000000ff",
                           headers={"Authorization": f"Bearer {access1}"})
    assert r.status_code == 404


def _sid(access_token: str) -> str:
    from app.auth.tokens import decode_access_token

    return decode_access_token(access_token)["sid"]


# ---- 管理员:用户审批 / 停用 / 角色 ----

@pytest.fixture
def admin_env(auth_client, auth_env):
    """在 auth_env 基础上造一个真管理员(走 create_admin 的同一条路径),并登录它。"""
    from scripts.create_admin import create_admin

    uid = create_admin(email="boss@example.com", password="Boss-Lab-2026!admin", display_name="老板")
    login = _login(auth_client, email="boss@example.com", password="Boss-Lab-2026!admin")
    assert login.status_code == 200
    token = login.json()["access_token"]
    auth_client.headers.update({"Authorization": f"Bearer {token}"})
    return {"client": auth_client, "token": token, "user_id": uid, "env": auth_env}


def test_admin_sees_users_and_approves(admin_env, auth_env):
    c = admin_env["client"]
    assert _register(c).status_code == 200
    _verify(c, auth_env.mailer)

    r = c.get("/api/v1/admin/users", params={"status": "pending_approval"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1 and body["items"][0]["email"] == EMAIL
    assert body["counts"].get("pending_approval") == 1
    uid = body["items"][0]["id"]

    r = c.post(f"/api/v1/admin/users/{uid}/approve", json={"note": "面谈通过"})
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    assert r.json()["review_note"] == "面谈通过"
    assert _login(c, password=PWD).status_code == 200

    # 重复审批:状态已经不是可审状态 → 409(而不是静默改写)
    r = c.post(f"/api/v1/admin/users/{uid}/approve", json={})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "invalid_user_state"


def test_admin_disable_kicks_user_out_immediately(admin_env, auth_env):
    c = admin_env["client"]
    login, uid = _seed_active(c, auth_env)
    access = login.json()["access_token"]
    assert c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access}"}).status_code == 200

    assert c.post(f"/api/v1/admin/users/{uid}/disable", json={"note": "违规"}).status_code == 200
    # auth_version 递增 + 会话撤销 → 在途 access token 立刻失效
    assert c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access}"}).status_code == 401
    r = _login(c, password=PWD)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "account_disabled"

    # 管理员误停用自己:拒绝
    r = c.post(f"/api/v1/admin/users/{admin_env['user_id']}/disable", json={})
    assert r.status_code == 409


def test_admin_role_change_guards(admin_env, auth_env):
    c = admin_env["client"]
    _, uid = _seed_active(c, auth_env)

    r = c.post(f"/api/v1/admin/users/{uid}/role", json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    # 不能改自己的角色
    r = c.post(f"/api/v1/admin/users/{admin_env['user_id']}/role", json={"role": "user"})
    assert r.status_code == 409
    # 不认识的角色
    r = c.post(f"/api/v1/admin/users/{uid}/role", json={"role": "root"})
    assert r.status_code == 409


def test_admin_audit_lists_actions(admin_env):
    c = admin_env["client"]
    r = c.get("/api/v1/admin/audit", params={"limit": 50})
    assert r.status_code == 200
    actions = [a["action"] for a in r.json()["items"]]
    assert "bootstrap_admin" in actions                    # create_admin 留了痕


def test_non_admin_cannot_touch_admin_endpoints(auth_client, auth_env):
    login, _ = _seed_active(auth_client, auth_env)
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    for path, body in (("/api/v1/admin/users", None),
                       ("/api/v1/admin/audit", None)):
        assert auth_client.get(path, headers=headers).status_code == 403
    r = auth_client.post("/api/v1/admin/users/whatever/approve", headers=headers, json={})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"


# ---- 审计:失败路径也必须在案 ----

def _audit_rows(auth_env) -> list[dict]:
    from app.auth import store

    with auth_env.db.session_scope() as session:
        return [{"action": r.action, "result": r.result, "note": r.note}
                for r in store.recent_audit(session, limit=200)]


def test_failed_login_leaves_an_audit_trail(auth_client, auth_env):
    """失败也要留痕 —— 而审计写在「紧接着抛异常」的路径上,必须走独立事务。

    这条用例正是钉住那个坑:审计若写在业务事务里,会被随后的 raise 回滚掉,一条都不剩。
    """
    _seed_active(auth_client, auth_env)
    assert _login(auth_client, password="Wrong-Password-9!").status_code == 401
    assert _login(auth_client, email="nobody@example.com").status_code == 401

    failed = [r for r in _audit_rows(auth_env) if r["action"] == "login_failed"]
    notes = {r["note"] for r in failed}
    assert "密码错误" in notes
    assert "账号不存在" in notes


def test_refresh_replay_is_audited_and_session_revoked(auth_client, auth_env):
    login, _ = _seed_active(auth_client, auth_env)
    old = auth_client.cookies.get("qa_refresh")
    assert _refresh(auth_client).status_code == 200
    auth_env.clock.advance(seconds=400)
    auth_client.cookies.set("qa_refresh", old)
    assert _refresh(auth_client).status_code == 401

    replayed = [r for r in _audit_rows(auth_env) if r["action"] == "refresh_replay"]
    assert len(replayed) == 1 and replayed[0]["result"] == "denied"


# ---- 配置缺失 / 限流设施不可用:一律明确报错,绝不降级 ----

def test_missing_secret_or_db_reports_503_not_anonymous(auth_client, auth_env, monkeypatch):
    monkeypatch.setattr(auth_env.settings, "auth_jwt_secret", "")
    r = _login(auth_client)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "auth_not_configured"

    monkeypatch.setattr(auth_env.settings, "metering_mysql_url", "")
    r = auth_client.post("/api/v1/auth/register",
                         json={"email": "a@b.com", "password": "Lab-QA-2026!strong"})
    assert r.status_code == 503


def test_rate_limit_unavailable_fails_closed(auth_client, auth_env, monkeypatch):
    """Redis 不可用 = 限流不可用 → 敏感接口 503(失败关闭),而不是「不计较、放行」。"""
    from app.auth import ratelimit

    monkeypatch.setattr(auth_env.settings, "redis_url", "redis://127.0.0.1:1/0")
    ratelimit.install(None)                      # 走真实的 RedisLimiter 构造路径
    r = _login(auth_client)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "rate_limit_unavailable"


def test_login_attempts_are_rate_limited(auth_client, auth_env):
    _seed_active(auth_client, auth_env)          # 这里已经登录过,也计入同一窗口
    codes = [_login(auth_client, password="Wrong-Password-9!").status_code for _ in range(11)]
    assert 429 in codes                          # 撞上「每邮箱 10 次 / 5 分钟」后开始拒绝
    r = _login(auth_client, password="Wrong-Password-9!")
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


# ---- 首个管理员的建立命令 ----

def test_create_admin_finishes_as_active_verified_admin(auth_env):
    """create_admin 必须走完状态机(否则会建出一个登不进去的账号)。"""
    from app.auth import store
    from scripts.create_admin import create_admin

    uid = create_admin(email="boss2@example.com", password="Boss-Lab-2026!admin")
    with auth_env.db.session_scope() as session:
        user = store.get_user_by_id(session, uid)
        assert (user.status, user.role) == ("active", "admin")
        assert user.email_verified_at is not None

    with pytest.raises(RuntimeError):            # 同名账号:拒绝,不改已有账号(尤其不改密码)
        create_admin(email="boss2@example.com", password="Another-Password-9!")


def test_create_admin_cli_reads_password_from_stdin(auth_env, monkeypatch, capsys):
    """命令行入口:密码只从 stdin 读(不进 shell 历史),弱口令照样被拒。"""
    import io

    from scripts import create_admin as cli

    monkeypatch.setattr("sys.stdin", io.StringIO("Cli-Password-2026!ok\n"))
    assert cli.main(["--email", "cli@example.com", "--password-stdin", "--name", "CLI"]) == 0
    assert "已建立管理员" in capsys.readouterr().out

    monkeypatch.setattr("sys.stdin", io.StringIO("123456\n"))
    assert cli.main(["--email", "cli2@example.com", "--password-stdin"]) == 2   # 弱口令
    assert "密码" in capsys.readouterr().err
