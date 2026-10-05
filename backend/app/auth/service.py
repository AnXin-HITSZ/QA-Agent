"""认证业务编排:注册 → 验证邮箱 → 管理员审批 → 登录 / 刷新 / 登出 / 改密 / 重置。

状态机(users.status,技术方案 §3):
    register ──► pending_email ──(点验证链接)──► pending_approval ──(管理员通过)──► active
                     │                                  │
                     └──────────(管理员拒绝)────────────┴──► rejected
    active ──(管理员禁用)──► disabled ──(管理员启用)──► active

    * 注册一律 role=user(公共注册接口不给选角色);
    * 「验证邮箱」只证明这个人能用这个邮箱,不等于批准使用 —— 两件事分开;
    * 只有 active(且已验证)才发凭证;状态不对时**先验证密码再报状态**,免得成了账号探测器;
    * 首个 admin 只能由命令行建立(scripts/create_admin.py),没有公开的管理员注册口。

事务与外部调用(技术方案 §7):每个方法一个短事务,事务里只碰数据库;
发信在事务**之后**做(寄信慢且可能失败,不能把行锁攥在手里)。

本模块是**同步 DB + 异步外壳**:所有同步工作经 app.auth.db.run() 进线程池,
事件循环不被 SQLAlchemy / Argon2 阻塞。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.auth import db, ratelimit, store, tokens
from app.auth.emails import InvalidEmail, normalize_email
from app.auth.errors import (
    AccountNotUsable, AuthNotConfigured, InvalidCredentials, InvalidDisplayName, InvalidPassword,
    InvalidToken, InvalidUserState, MailSendFailed, TokenReplayed, UserNotFound,
)
from app.auth.mailer import FakeMailer, get_mailer
from app.auth.passwords import (
    check_password, dummy_verify, hash_password, needs_rehash, verify_password,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

# 邮箱重发的最小间隔(秒):防止有人拿注册 / 找回接口当发信机。
RESEND_MIN_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class Principal:
    """已通过访问令牌校验的调用者(每个请求一份,deps.py 负责构造)。"""

    user_id: str
    email: str
    display_name: str
    role: str
    status: str
    auth_version: int
    session_id: str

    @property
    def is_admin(self) -> bool:
        return self.role == store.ROLE_ADMIN


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    user: dict          # 前端直接可用的用户信息(不含任何令牌摘要 / 密码哈希)


def public_user(user) -> dict:
    """对外可返回的用户信息(白名单字段:绝不整行 dump —— 行里有 password_hash)。"""
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
        "email_verified": user.email_verified_at is not None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def admin_user_view(user) -> dict:
    """管理员看到的用户信息:比 public_user 多出审批痕迹(审批人 / 时间 / 备注)。

    依然过白名单,不含 password_hash / 任何令牌。
    """
    view = public_user(user)
    view.update({
        "reviewed_at": user.reviewed_at.isoformat() if user.reviewed_at else None,
        "reviewed_by": user.reviewed_by,
        "review_note": user.review_note or "",
    })
    return view


class AuthService:
    """认证服务。settings 与 now 可注入,便于测试控制时间与有效期。"""

    def __init__(self, *, now=None) -> None:
        self._now = now or db.utc_naive

    def now(self) -> datetime:
        return self._now()

    # ---- 注册 / 邮箱验证 ----

    async def register(self, *, email: str, password: str, display_name: str, ip: str) -> str:
        """注册。返回给用户的文案一律中性(不透露该邮箱是否已注册)。

        邮箱格式 / 密码策略的问题会直接报错(那是对输入本身的反馈,不涉账号存在性)。
        """
        norm = normalize_email(email)                      # 不合法 → InvalidEmail(400)
        self._check_password(password)
        name = self._display_name(display_name, norm)
        mailer = self._mailer()                            # 发信没配就先报错,别建半截账号

        decision = await db.run(self._register_sync, norm, password, name, ip)
        if decision in ("created", "resend"):
            token = await db.run(self._issue_verify_token_sync, norm, ip)
            await self._send_verify(mailer, to=norm, link=self._link("/verify-email", token))
            return "注册成功。我们已发送验证邮件,请先点开邮件里的链接完成验证。"
        # "exists_other":邮箱已注册且不在待验证阶段 —— 不寄信,同样中性回复。
        return "如果该邮箱可以注册,我们已经发送了一封验证邮件。"

    def _register_sync(self, email: str, password: str, name: str, ip: str) -> str:
        now = self.now()
        with db.session_scope() as session:
            existing = store.get_user_by_email(session, email)
            if existing is None:
                store.create_user(session, user_id=tokens.new_id(), email=email,
                                  password_hash=hash_password(password),
                                  display_name=name, now=now)
                store.add_audit(session, action="register", result="ok", now=now, ip=ip,
                                target_user_id=None, email=email, note="待验证邮箱")
                return "created"
            if existing.status == store.STATUS_PENDING_EMAIL:
                last = store.last_email_token_at(session, user_id=existing.id,
                                                 purpose=store.PURPOSE_VERIFY)
                if last is not None and (now - last).total_seconds() < RESEND_MIN_INTERVAL_SECONDS:
                    # 短时间内重复注册:当作已受理,不再发信(也不暴露账号状态)。
                    return "exists_other"
                store.add_audit(session, action="register", result="ok", now=now, ip=ip,
                                target_user_id=existing.id, email=email, note="重复注册,重发验证")
                return "resend"
            store.add_audit(session, action="register", result="denied", now=now, ip=ip,
                            target_user_id=existing.id, email=email,
                            note=f"重复注册(当前状态 {existing.status})")
            return "exists_other"

    async def resend_verification(self, *, email: str, ip: str) -> None:
        """重发验证邮件。中性返回:无论邮箱是否存在都不透露。"""
        try:
            norm = normalize_email(email)
        except InvalidEmail:
            return
        mailer = self._mailer()
        try:
            token = await db.run(self._resend_verify_sync, norm, ip)
        except RateLimitedForResend:
            return                       # 刚发过:静默忽略,对外一致
        if token:
            await self._send_verify(mailer, to=norm, link=self._link("/verify-email", token))

    def _resend_verify_sync(self, email: str, ip: str) -> str | None:
        now = self.now()
        with db.session_scope() as session:
            user = store.get_user_by_email(session, email)
            if user is None or user.status != store.STATUS_PENDING_EMAIL:
                return None
            last = store.last_email_token_at(session, user_id=user.id, purpose=store.PURPOSE_VERIFY)
            if last is not None and (now - last).total_seconds() < RESEND_MIN_INTERVAL_SECONDS:
                raise RateLimitedForResend()
            store.consume_outstanding_email_tokens(session, user_id=user.id,
                                                   purpose=store.PURPOSE_VERIFY, now=now)
            raw, digest = tokens.new_random_token()
            store.add_email_token(session, token_id=tokens.new_id(), user_id=user.id,
                                  purpose=store.PURPOSE_VERIFY, token_hash=digest, now=now,
                                  expires_at=now + timedelta(
                                      minutes=max(1, int(get_settings().auth_verify_token_minutes))),
                                  ip=ip)
            store.add_audit(session, action="verify_email", result="ok", now=now, ip=ip,
                            target_user_id=user.id, email=email, note="重发验证邮件")
            return raw

    def _issue_verify_token_sync(self, email: str, ip: str) -> str:
        """注册成功后的第一封验证信(新签一枚令牌,作废旧令牌)。"""
        now = self.now()
        with db.session_scope() as session:
            user = store.get_user_by_email(session, email)
            assert user is not None, "register() 刚建的用户必须能查到"
            store.consume_outstanding_email_tokens(session, user_id=user.id,
                                                   purpose=store.PURPOSE_VERIFY, now=now)
            raw, digest = tokens.new_random_token()
            store.add_email_token(session, token_id=tokens.new_id(), user_id=user.id,
                                  purpose=store.PURPOSE_VERIFY, token_hash=digest, now=now,
                                  expires_at=now + timedelta(
                                      minutes=max(1, int(get_settings().auth_verify_token_minutes))),
                                  ip=ip)
            return raw

    async def verify_email(self, *, token: str, ip: str) -> dict:
        """消费验证令牌,把账号推进到「待审批」;重复点链接按幂等处理。"""
        if not token:
            raise InvalidToken("验证链接不完整")
        return await db.run(self._verify_email_sync, token, ip)

    def _verify_email_sync(self, raw_token: str, ip: str) -> dict:
        now = self.now()
        digest = tokens.sha256_hex(raw_token)
        with db.session_scope() as session:
            row = store.find_email_token(session, token_hash=digest,
                                         purpose=store.PURPOSE_VERIFY, for_update=True)
            if row is None:
                raise InvalidToken("验证链接无效")
            user = store.get_user_by_id(session, row.user_id)
            if user is None:
                raise InvalidToken("验证链接无效")
            if row.consumed_at is not None:
                # 同一枚令牌被再次提交:多半是刷新页面 / 点了两次。已完成的验证不重复执行,
                # 但也不报「无效」——按当前状态如实回话(不泄露任何额外信息)。
                raise InvalidToken("验证链接已使用过")
            if row.expires_at <= now:
                raise InvalidToken("验证链接已过期,请重新发送验证邮件")
            if not store.consume_email_token(session, token_id=row.id, now=now):
                raise InvalidToken("验证链接已使用过")
            store.mark_email_verified(session, user.id, now)
            store.add_audit(session, action="verify_email", result="ok", now=now, ip=ip,
                            target_user_id=user.id, email=user.email, note="邮箱已验证,待管理员审批")
        return {"status": store.STATUS_PENDING_APPROVAL,
                "message": "邮箱验证通过。账号需要管理员审批后才能使用。"}

    # ---- 登录 / 刷新 / 登出 ----

    async def login(self, *, email: str, password: str, ip: str, user_agent: str) -> IssuedTokens:
        try:
            norm = normalize_email(email)
        except InvalidEmail as exc:
            dummy_verify(password)
            raise InvalidCredentials("邮箱或密码不正确") from exc
        return await db.run(self._login_sync, norm, password, ip, user_agent)

    def _login_sync(self, email: str, password: str, ip: str, user_agent: str) -> IssuedTokens:
        now = self.now()
        with db.session_scope() as session:
            user = store.get_user_by_email(session, email)
            if user is None:
                dummy_verify(password)                 # 抹平时序差:有没有这个账号都一样慢
                self._audit_now("login_failed", result="failed", ip=ip, email=email,
                                note="账号不存在")
                raise InvalidCredentials("邮箱或密码不正确")
            if not verify_password(user.password_hash, password):
                self._audit_now("login_failed", result="failed", ip=ip, target_user_id=user.id,
                                email=email, note="密码错误")
                raise InvalidCredentials("邮箱或密码不正确")
            # 密码已对,才谈状态 —— 不知道密码的人无法用这里探测账号状态。
            if user.status != store.STATUS_ACTIVE:
                self._audit_now("login_failed", result="denied", ip=ip, target_user_id=user.id,
                                email=email, note=f"状态不允许登录:{user.status}")
                raise AccountNotUsable(user.status, _status_message(user.status))
            if user.email_verified_at is None:         # 理论到不了;状态与验证记录不一致时兜底
                raise AccountNotUsable(store.STATUS_PENDING_EMAIL, _status_message(store.STATUS_PENDING_EMAIL))

            if needs_rehash(user.password_hash):       # 参数升级:登录成功后顺手重哈希
                # 只换哈希、不动 auth_version:密码没变,不该把用户正在用的其他设备踢下线。
                store.update_password_hash_only(session, user.id,
                                                password_hash=hash_password(password), now=now)

            issued = self._new_session(session, user=user, ip=ip, user_agent=user_agent, now=now)
            store.touch_last_login(session, user.id, now)
            store.add_audit(session, action="login", result="ok", now=now, ip=ip,
                            actor_user_id=user.id, target_user_id=user.id, email=email, note="登录成功")
            return issued

    def _new_session(self, session, *, user, ip: str, user_agent: str, now: datetime) -> IssuedTokens:
        """建一条登录会话 + 第一枚刷新令牌,并签发 access token。"""
        settings = get_settings()
        session_id = tokens.new_id()
        expires_at = now + timedelta(days=max(1, int(settings.auth_refresh_token_days)))
        store.create_session(session, session_id=session_id, user_id=user.id,
                             user_agent=user_agent, ip=ip, now=now, expires_at=expires_at)
        refresh_raw, refresh_digest = tokens.new_random_token()
        store.add_refresh_token(session, token_id=tokens.new_id(), session_id=session_id,
                                user_id=user.id, token_hash=refresh_digest, now=now)
        access, access_exp = tokens.encode_access_token(
            user_id=user.id, session_id=session_id, auth_version=int(user.auth_version))
        return IssuedTokens(access_token=access, access_expires_at=access_exp,
                            refresh_token=refresh_raw, refresh_expires_at=expires_at,
                            user=public_user(user))

    async def refresh(self, *, refresh_token: str, ip: str) -> IssuedTokens:
        """用刷新令牌换一对新令牌(轮换:旧的立即作废)。

        重放判定见 _refresh_sync 的注释:超出宽限窗口再用已消费的令牌 = 有人拿着旧令牌,
        撤销整个会话。
        """
        if not refresh_token:
            raise InvalidToken("缺少刷新令牌")
        return await db.run(self._refresh_sync, refresh_token, ip)

    def _refresh_sync(self, raw_token: str, ip: str) -> IssuedTokens:
        settings = get_settings()
        now = self.now()
        digest = tokens.sha256_hex(raw_token)
        grace = max(0, int(settings.auth_refresh_grace_seconds))
        with db.session_scope() as session:
            row = store.find_refresh_token(session, digest, for_update=True)
            if row is None:
                raise InvalidToken("登录状态已失效,请重新登录")
            sess = store.get_session(session, row.session_id)
            user = store.get_user_by_id(session, row.user_id)
            if sess is None or user is None:
                raise InvalidToken("登录状态已失效,请重新登录")
            if sess.revoked_at is not None or sess.expires_at <= now:
                raise InvalidToken("登录状态已失效,请重新登录")
            if user.status != store.STATUS_ACTIVE or user.email_verified_at is None:
                # 用户被停用 / 状态回退:这个会话不该再换出新令牌。撤销必须独立成事务 ——
                # 紧接着就抛异常,写在业务事务里会被回滚掉(见 _revoke_session_now 的注释)。
                self._revoke_session_now(sess.id, reason="user_not_active")
                raise InvalidToken("登录状态已失效,请重新登录")

            if row.consumed_at is not None:
                # 已被换掉的旧令牌又被提交:两种情况。
                age = (now - row.consumed_at).total_seconds()
                if age <= grace:
                    # ① 宽限窗口内:前端并发刷新 / 网络重试(同一枚令牌发两次)。
                    #    放行并再发一枚 —— 不撤销会话,否则用户会被自己的前端踢下线。
                    logger.info("刷新令牌在宽限窗口内被重复提交(session=%s, %.1fs 前)",
                                sess.id, age)
                else:
                    # ② 超出窗口:旧令牌很可能已泄露并被别人拿去用 —— 撤销整个会话(家族),
                    #    并要求重新登录。正常前端不会走到这里。
                    #    撤销与审计各走独立事务:下一行就抛异常,跟着业务事务会被回滚。
                    self._revoke_session_now(sess.id, reason="rotated_replay")
                    self._audit_now("refresh_replay", result="denied", ip=ip,
                                    actor_user_id=user.id, target_user_id=user.id,
                                    email=user.email, note=f"旧刷新令牌被重复使用({age:.0f}s 前已轮换)")
                    raise TokenReplayed("检测到登录状态异常,已为你退出全部会话,请重新登录")
            elif not store.consume_refresh_token(session, token_id=row.id, now=now):
                raise InvalidToken("登录状态已失效,请重新登录")

            store.touch_session(session, sess.id, now)
            new_raw, new_digest = tokens.new_random_token()
            store.add_refresh_token(session, token_id=tokens.new_id(), session_id=sess.id,
                                    user_id=user.id, token_hash=new_digest, now=now)
            access, access_exp = tokens.encode_access_token(
                user_id=user.id, session_id=sess.id, auth_version=int(user.auth_version))
            return IssuedTokens(access_token=access, access_expires_at=access_exp,
                                refresh_token=new_raw, refresh_expires_at=sess.expires_at,
                                user=public_user(user))

    def _revoke_session_sync(self, session, session_id: str, *, reason: str, now: datetime) -> None:
        store.revoke_session(session, session_id=session_id, reason=reason, now=now)
        store.revoke_refresh_tokens(session, session_id=session_id, now=now)

    async def logout(self, *, session_id: str, user_id: str, ip: str, refresh_token: str = "") -> None:
        """登出当前设备:撤销这一条会话(令牌链一并作废)。已撤销的按幂等处理。"""
        await db.run(self._logout_sync, session_id, user_id, ip, refresh_token)

    def _logout_sync(self, session_id: str, user_id: str, ip: str, refresh_token: str) -> None:
        now = self.now()
        with db.session_scope() as session:
            sess = store.get_session(session, session_id) if session_id else None
            if sess is None and refresh_token:
                row = store.find_refresh_token(session, tokens.sha256_hex(refresh_token))
                sess = store.get_session(session, row.session_id) if row else None
            if sess is None or sess.user_id != user_id:
                return                                  # 幂等:没这条会话就什么都不做
            self._revoke_session_sync(session, sess.id, reason="logout", now=now)
            store.add_audit(session, action="logout", result="ok", now=now, ip=ip,
                            actor_user_id=user_id, target_user_id=user_id, note="登出当前设备")

    async def logout_all(self, *, user_id: str, ip: str) -> int:
        """登出全部设备:撤销所有会话 + auth_version += 1(在途 access token 立即失效)。"""
        return await db.run(self._logout_all_sync, user_id, ip)

    def _logout_all_sync(self, user_id: str, ip: str) -> int:
        now = self.now()
        with db.session_scope() as session:
            user = store.get_user_by_id(session, user_id)
            if user is None:
                return 0
            count = store.revoke_all_sessions(session, user_id=user_id, reason="logout_all", now=now)
            # 会话行撤销后刷新令牌本身就换不出东西了;再递增 auth_version,让在途的
            # access token(最长 15 分钟)也立即作废。
            store.bump_auth_version(session, user_id, now)
            store.add_audit(session, action="logout_all", result="ok", now=now, ip=ip,
                            actor_user_id=user_id, target_user_id=user_id, email=user.email,
                            note=f"登出全部设备({count} 个会话)")
            return count

    # ---- 密码 ----

    async def change_password(self, *, user_id: str, current_password: str,
                              new_password: str, ip: str) -> None:
        self._check_password(new_password)
        await db.run(self._change_password_sync, user_id, current_password, new_password, ip)

    def _change_password_sync(self, user_id: str, current: str, new: str, ip: str) -> None:
        now = self.now()
        with db.session_scope() as session:
            user = store.get_user_by_id(session, user_id)
            if user is None or not verify_password(user.password_hash, current):
                self._audit_now("password_change", result="denied", ip=ip, actor_user_id=user_id,
                                target_user_id=user_id, note="当前密码不正确")
                raise InvalidCredentials("当前密码不正确")
            if verify_password(user.password_hash, new):
                raise InvalidCredentials("新密码不能与当前密码相同")
            store.update_password(session, user_id, password_hash=hash_password(new), now=now)
            revoked = store.revoke_all_sessions(session, user_id=user_id,
                                                reason="password_change", now=now)
            store.add_audit(session, action="password_change", result="ok", now=now, ip=ip,
                            actor_user_id=user_id, target_user_id=user_id, email=user.email,
                            note=f"改密成功,撤销 {revoked} 个会话")

    async def forgot_password(self, *, email: str, ip: str) -> None:
        """发重置邮件。中性返回:邮箱不存在也照样「已发送」。"""
        try:
            norm = normalize_email(email)
        except InvalidEmail:
            return
        mailer = self._mailer()
        try:
            token = await db.run(self._issue_reset_token_sync, norm, ip)
        except RateLimitedForResend:
            return
        if token:
            await self._send_reset(mailer, to=norm, link=self._link("/reset-password", token))

    def _issue_reset_token_sync(self, email: str, ip: str) -> str | None:
        now = self.now()
        with db.session_scope() as session:
            user = store.get_user_by_email(session, email)
            if user is None or user.status in (store.STATUS_REJECTED, store.STATUS_DISABLED):
                return None
            last = store.last_email_token_at(session, user_id=user.id, purpose=store.PURPOSE_RESET)
            if last is not None and (now - last).total_seconds() < RESEND_MIN_INTERVAL_SECONDS:
                raise RateLimitedForResend()
            store.consume_outstanding_email_tokens(session, user_id=user.id,
                                                   purpose=store.PURPOSE_RESET, now=now)
            raw, digest = tokens.new_random_token()
            store.add_email_token(session, token_id=tokens.new_id(), user_id=user.id,
                                  purpose=store.PURPOSE_RESET, token_hash=digest, now=now,
                                  expires_at=now + timedelta(
                                      minutes=max(1, int(get_settings().auth_reset_token_minutes))),
                                  ip=ip)
            store.add_audit(session, action="password_reset", result="ok", now=now, ip=ip,
                            target_user_id=user.id, email=email, note="发出重置密码邮件")
            return raw

    async def reset_password(self, *, token: str, new_password: str, ip: str) -> dict:
        self._check_password(new_password)
        if not token:
            raise InvalidToken("重置链接不完整")
        return await db.run(self._reset_password_sync, token, new_password, ip)

    def _reset_password_sync(self, raw_token: str, new_password: str, ip: str) -> dict:
        now = self.now()
        digest = tokens.sha256_hex(raw_token)
        with db.session_scope() as session:
            row = store.find_email_token(session, token_hash=digest,
                                         purpose=store.PURPOSE_RESET, for_update=True)
            if row is None or row.consumed_at is not None:
                raise InvalidToken("重置链接无效或已使用")
            if row.expires_at <= now:
                raise InvalidToken("重置链接已过期,请重新申请")
            user = store.get_user_by_id(session, row.user_id)
            if user is None:
                raise InvalidToken("重置链接无效")
            if not store.consume_email_token(session, token_id=row.id, now=now):
                raise InvalidToken("重置链接已使用")
            store.update_password(session, user_id=user.id,
                                  password_hash=hash_password(new_password), now=now)
            revoked = store.revoke_all_sessions(session, user_id=user.id,
                                                reason="password_reset", now=now)
            store.add_audit(session, action="password_reset", result="ok", now=now, ip=ip,
                            actor_user_id=user.id, target_user_id=user.id, email=user.email,
                            note=f"重置密码成功,撤销 {revoked} 个会话")
        # 重置**不**顺带验证邮箱、也不改审批状态:两件事各有各的语义(技术方案 §3)。
        return {"message": "密码已重置,请用新密码登录。"}

    # ---- 访问令牌校验(每个已认证请求走一次)----

    async def load_principal(self, *, user_id: str, session_id: str) -> Principal | None:
        """按 access token 里的 sub / sid 回库核对:用户仍 active、会话未撤销未过期。

        返回 None 表示「令牌虽然验签通过,但状态已经不允许」——调用方一律 401。
        回库核对是刻意的:只验签的话,禁用 / 改密 / 登出都无法立即生效。
        """
        return await db.run(self._load_principal_sync, user_id, session_id)

    def _load_principal_sync(self, user_id: str, session_id: str) -> Principal | None:
        now = self.now()
        with db.session_scope() as session:
            row = store.load_principal(session, user_id=user_id, session_id=session_id)
            if row is None:
                return None
            user, sess = row
            if (user.status != store.STATUS_ACTIVE or user.email_verified_at is None
                    or sess.revoked_at is not None or sess.expires_at <= now):
                return None
            return Principal(user_id=user.id, email=user.email, display_name=user.display_name,
                             role=user.role, status=user.status,
                             auth_version=int(user.auth_version), session_id=sess.id)

    async def get_profile(self, *, user_id: str) -> dict | None:
        """当前用户的自述信息(/auth/me)。返回 None = 行没了(被删号),调用方 401。"""
        return await db.run(self._get_profile_sync, user_id)

    def _get_profile_sync(self, user_id: str) -> dict | None:
        with db.session_scope() as session:
            row = store.get_user_by_id(session, user_id)
            return public_user(row) if row is not None else None

    # ---- 会话列表 ----

    async def list_sessions(self, *, user_id: str) -> list[dict]:
        return await db.run(self._list_sessions_sync, user_id)

    def _list_sessions_sync(self, user_id: str) -> list[dict]:
        now = self.now()
        with db.session_scope() as session:
            rows = store.list_sessions(session, user_id=user_id, now=now)
            return [{"id": r.id, "user_agent": r.user_agent, "ip": r.ip,
                     "created_at": r.created_at.isoformat(),
                     "last_used_at": r.last_used_at.isoformat(),
                     "expires_at": r.expires_at.isoformat()} for r in rows]

    # ---- 管理员:用户管理 ----

    async def list_users(self, *, status: str | None, q: str, offset: int, limit: int) -> dict:
        return await db.run(self._list_users_sync, status, q, offset, limit)

    def _list_users_sync(self, status: str | None, q: str, offset: int, limit: int) -> dict:
        with db.session_scope() as session:
            rows, total = store.list_users(session, status=status, q=q, offset=offset, limit=limit)
            counts = store.count_users_by_status(session)
            items = [admin_user_view(r) for r in rows]
        return {"items": items, "total": total, "counts": counts}

    async def review_user(self, *, admin: Principal, target_id: str, approve: bool,
                          note: str, ip: str) -> dict:
        """审批:通过 → active;拒绝 → rejected。只在「待验证 / 待审批」状态上生效。"""
        return await db.run(self._review_sync, admin, target_id, approve, note, ip)

    def _review_sync(self, admin: Principal, target_id: str, approve: bool,
                     note: str, ip: str) -> dict:
        now = self.now()
        with db.session_scope() as session:
            target = store.get_user_by_id(session, target_id)
            if target is None:
                raise UserNotFound()
            changed = store.set_review(session, target_id, admin_id=admin.user_id,
                                       approve=approve, note=note, now=now)
            action = "approve" if approve else "reject"
            if not changed:
                # 状态已被别的管理员改掉:记「撞车」并拒绝(审计走独立事务 —— 这就抛异常了)
                self._audit_now(action, result="denied", ip=ip, actor_user_id=admin.user_id,
                                target_user_id=target_id, email=target.email,
                                note=f"状态已变为 {target.status},未生效")
                raise InvalidUserState(f"该账号当前状态是 {target.status},不能审批")
            store.add_audit(session, action=action, result="ok", now=now, ip=ip,
                            actor_user_id=admin.user_id, target_user_id=target_id,
                            email=target.email, note=note or ("审批通过" if approve else "审批拒绝"))
            session.refresh(target)
            return admin_user_view(target)

    async def set_user_enabled(self, *, admin: Principal, target_id: str, enable: bool,
                               note: str, ip: str) -> dict:
        """禁用 / 重新启用。禁用会同时撤销该用户全部会话并递增 auth_version(立即下线)。"""
        if target_id == admin.user_id:
            raise InvalidUserState("不能停用自己的账号")
        return await db.run(self._set_enabled_sync, admin, target_id, enable, note, ip)

    def _set_enabled_sync(self, admin: Principal, target_id: str, enable: bool,
                          note: str, ip: str) -> dict:
        now = self.now()
        target_status = store.STATUS_ACTIVE if enable else store.STATUS_DISABLED
        with db.session_scope() as session:
            target = store.get_user_by_id(session, target_id)
            if target is None:
                raise UserNotFound()
            changed = store.set_user_status(session, target_id, status=target_status,
                                            note=note, now=now)
            action = "enable" if enable else "disable"
            if not changed:
                self._audit_now(action, result="denied", ip=ip, actor_user_id=admin.user_id,
                                target_user_id=target_id, email=target.email,
                                note=f"状态是 {target.status},不能{'启用' if enable else '停用'}")
                raise InvalidUserState(f"该账号当前状态是 {target.status},不能{'启用' if enable else '停用'}")
            store.add_audit(session, action=action, result="ok", now=now, ip=ip,
                            actor_user_id=admin.user_id, target_user_id=target_id,
                            email=target.email, note=note or ("启用账号" if enable else "停用账号"))
            if not enable:
                revoked = store.revoke_all_sessions(session, user_id=target_id,
                                                    reason="admin_disable", now=now)
                store.bump_auth_version(session, target_id, now)
                store.add_audit(session, action="disable", result="ok", now=now, ip=ip,
                                actor_user_id=admin.user_id, target_user_id=target_id,
                                email=target.email, note=f"停用同时撤销 {revoked} 个会话")
            session.refresh(target)
            return admin_user_view(target)

    async def change_role(self, *, admin: Principal, target_id: str, role: str, ip: str) -> dict:
        """改角色(只认识 user / admin)。不能改自己 —— 免得唯一的 admin 自锁在门外。"""
        if role not in (store.ROLE_USER, store.ROLE_ADMIN):
            raise InvalidUserState("角色只能是 user 或 admin")
        if target_id == admin.user_id:
            raise InvalidUserState("不能修改自己的角色")
        return await db.run(self._change_role_sync, admin, target_id, role, ip)

    def _change_role_sync(self, admin: Principal, target_id: str, role: str, ip: str) -> dict:
        now = self.now()
        with db.session_scope() as session:
            target = store.get_user_by_id(session, target_id)
            if target is None:
                raise UserNotFound()
            if target.status != store.STATUS_ACTIVE:
                raise InvalidUserState(f"该账号当前状态是 {target.status},只有 active 的账号能改角色")
            if target.role == role:
                session.refresh(target)
                return admin_user_view(target)          # 幂等:没变就照原样返回
            store.set_user_role(session, target_id, role=role, now=now)
            store.add_audit(session, action="role_change", result="ok", now=now, ip=ip,
                            actor_user_id=admin.user_id, target_user_id=target_id,
                            email=target.email, note=f"{target.role} → {role}")
            session.refresh(target)
            return admin_user_view(target)

    async def revoke_session(self, *, user_id: str, session_id: str, ip: str) -> bool:
        return await db.run(self._revoke_session_for_user_sync, user_id, session_id, ip)

    def _revoke_session_for_user_sync(self, user_id: str, session_id: str, ip: str) -> bool:
        now = self.now()
        with db.session_scope() as session:
            sess = store.get_session(session, session_id)
            if sess is None or sess.user_id != user_id:
                return False                             # 不是自己的会话:当作不存在
            self._revoke_session_sync(session, session_id, reason="logout", now=now)
            store.add_audit(session, action="logout", result="ok", now=now, ip=ip,
                            actor_user_id=user_id, target_user_id=user_id, note="登出指定设备")
            return True

    async def audit_log(self, *, limit: int, target_user_id: str | None = None) -> list[dict]:
        return await db.run(self._audit_log_sync, limit, target_user_id)

    def _audit_log_sync(self, limit: int, target_user_id: str | None) -> list[dict]:
        with db.session_scope() as session:
            rows = store.recent_audit(session, limit=limit, target_user_id=target_user_id)
            return [{"at": r.occurred_at.isoformat(), "action": r.action, "result": r.result,
                     "actor_user_id": r.actor_user_id, "target_user_id": r.target_user_id,
                     "email": r.email, "ip": r.ip, "note": r.note} for r in rows]

    # ---- 内部小工具 ----

    def _check_password(self, password: str) -> None:
        check_password(password)        # 不合格 → InvalidPassword(文案可直接展示)

    # ---- 拒绝路径专用的「独立事务」写入口 ----

    def _audit_now(self, action: str, *, result: str, ip: str = "",
                   actor_user_id: str | None = None, target_user_id: str | None = None,
                   email: str = "", note: str = "") -> None:
        """用一个**独立的小事务**写审计 —— 专给「紧接着就要抛异常」的路径。

        为什么不能跟着业务事务走:db.session_scope 的语义与计量一致 —— 出异常即回滚。
        而「登录失败」「令牌重放」「审批撞车」这些事件恰恰长在抛异常的路径上,写在业务事务里
        会跟着一起消失(最坏情况:登录失败一条痕迹都留不下)。审计写失败只记日志,绝不改变
        对外结果:审计是旁路,不该成为新的失败点。
        """
        now = self.now()
        try:
            with db.session_scope() as session:
                store.add_audit(session, action=action, result=result, now=now, ip=ip,
                                actor_user_id=actor_user_id, target_user_id=target_user_id,
                                email=email, note=note)
        except Exception as exc:  # noqa: BLE001
            logger.warning("审计写入失败(不影响本次结果):action=%s %s", action, exc)

    def _revoke_session_now(self, session_id: str, *, reason: str) -> None:
        """独立事务撤销会话(连带作废其刷新令牌链):同样用于「拒绝后立刻抛异常」的路径。

        与 _audit_now 不同,这里**故意**让异常往外冒:撤销失败宁可报 500(响亮),
        也不能返回一个「看起来处理过了」成功响应。
        """
        now = self.now()
        with db.session_scope() as session:
            store.revoke_session(session, session_id=session_id, reason=reason, now=now)
            store.revoke_refresh_tokens(session, session_id=session_id, now=now)

    @staticmethod
    def _display_name(raw: str | None, email: str) -> str:
        name = (raw or "").strip()
        if not name:
            name = email.split("@", 1)[0][:64]
        if any(ord(ch) < 32 for ch in name):
            raise InvalidDisplayName("昵称不能包含控制字符")
        return name[:64]

    def _link(self, path: str, token: str) -> str:
        base = (get_settings().auth_frontend_base_url or "").rstrip("/")
        return f"{base}{path}?token={token}"

    def _mailer(self):
        return get_mailer()          # 未配置 → MailNotConfigured(503,明确报错)

    # provider=fake 时邮件只留在内存里 —— 本地演练要是也不落日志,就没人拿得到验证 / 重置
    # 令牌(端点不会把令牌回显给客户端,那是极危险的降级)。所以只在这种「本来就没打算真发」
    # 且明确是本地环境时把链接打进日志。生产环境(APP_ENV=prod 等)永不打:日志里的令牌
    # 就是明文凭证。白名单而非黑名单 —— 没写清楚的 env 一律按生产对待。
    _DEV_ENVS = frozenset({"dev", "local", "test"})

    def _log_dev_link(self, mailer, *, kind: str, to: str, link: str) -> None:
        if not isinstance(mailer, FakeMailer):
            return
        if str(get_settings().app_env or "").strip().lower() not in self._DEV_ENVS:
            return
        logger.info("[本地演练] MAIL_PROVIDER=fake,%s邮件未真正发出;收件人=%s 链接=%s",
                    kind, _mask(to), link)

    async def _send_verify(self, mailer, *, to: str, link: str) -> None:
        subject = "验证你的邮箱 · QA-Agent 实验室助手"
        text = (
            "你好,\n\n"
            "有人用这个邮箱注册了 QA-Agent 实验室助手。请点下面的链接完成邮箱验证"
            f"(链接 {get_settings().auth_verify_token_minutes} 分钟内有效,只能使用一次):\n\n"
            f"{link}\n\n"
            "验证后账号还需要管理员审批才能使用。\n"
            "如果这不是你本人操作,忽略本邮件即可,账号不会生效。\n"
        )
        try:
            await mailer.send(to=to, subject=subject, text=text)
            self._log_dev_link(mailer, kind="验证", to=to, link=link)
        except MailSendFailed as exc:
            logger.error("验证邮件发送失败:to=%s err=%s", _mask(to), exc)
            raise MailSendFailed("验证邮件发送失败,请稍后重试") from exc
        except AuthNotConfigured:
            raise
        except Exception as exc:  # noqa: BLE001 —— 未知异常同样按发信失败处理(不建半截账号)
            logger.exception("验证邮件发送异常")
            raise MailSendFailed(f"验证邮件发送失败:{type(exc).__name__}") from exc

    async def _send_reset(self, mailer, *, to: str, link: str) -> None:
        subject = "重置密码 · QA-Agent 实验室助手"
        text = (
            "你好,\n\n"
            "我们收到了重置这个邮箱账号密码的请求。请点下面的链接设置新密码"
            f"(链接 {get_settings().auth_reset_token_minutes} 分钟内有效,只能使用一次):\n\n"
            f"{link}\n\n"
            "重置后该账号在所有设备上的登录都会失效,需要重新登录。\n"
            "如果这不是你本人操作,请忽略本邮件 —— 你的密码不会被改动。\n"
        )
        try:
            await mailer.send(to=to, subject=subject, text=text)
            self._log_dev_link(mailer, kind="重置", to=to, link=link)
        except MailSendFailed as exc:
            # 找回密码**不能**把发信失败告诉匿名调用者(那会泄露账号是否存在),
            # 只记日志与审计,对外仍是中性回复。运维看日志。
            logger.error("重置邮件发送失败:to=%s err=%s", _mask(to), exc)
        except AuthNotConfigured:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("重置邮件发送异常")


class RateLimitedForResend(Exception):
    """同一邮箱短时间内的重复发信:静默忽略(对外仍是中性回复)。"""


def _status_message(status: str) -> str:
    return {
        store.STATUS_PENDING_EMAIL: "邮箱尚未验证,请先点开验证邮件里的链接",
        store.STATUS_PENDING_APPROVAL: "账号正在等待管理员审批,通过后即可登录",
        store.STATUS_REJECTED: "账号未通过审批,如有疑问请联系实验室管理员",
        store.STATUS_DISABLED: "账号已被停用,请联系实验室管理员",
    }.get(status, "账号当前不可用,请联系实验室管理员")


def _mask(email: str) -> str:
    from app.auth.emails import mask_email

    return mask_email(email)
