"""认证 / 用户管理 / 会话目录的全部 SQL 读写:同步、短事务、一次一个 session_scope。

约定(与 app/metering/store.py 一致):
- 函数都是**同步**的,由 service / 路由经 app.auth.db.run() 丢进线程池执行;
- 一次业务操作开一个短事务,提交即结束;事务里绝不发信、绝不调模型;
- 需要「检查 + 修改」原子完成的地方一律用带条件的 UPDATE(compare-and-set),
  先 SELECT 再判断会在多 worker 下翻车;
- 排序 / 分页一律下推到 SQL,**不做**全表扫描后在 Python 里过滤。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update

from app.auth.tables import (
    AuthAuditRow, AuthRefreshTokenRow, AuthSessionRow, ConversationRow, EmailTokenRow, UserRow,
)

# ---- 状态常量(与迁移脚本的 COMMENT 一致)----

STATUS_PENDING_EMAIL = "pending_email"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_ACTIVE = "active"
STATUS_REJECTED = "rejected"
STATUS_DISABLED = "disabled"

ROLE_USER = "user"
ROLE_ADMIN = "admin"

# 审批可作用的起始状态:还没被审批过、也还没被禁用的账号。
REVIEWABLE_STATUSES = (STATUS_PENDING_EMAIL, STATUS_PENDING_APPROVAL)

CONV_ACTIVE = "active"
CONV_DELETING = "deleting"
CONV_DELETED = "deleted"

PURPOSE_VERIFY = "verify_email"
PURPOSE_RESET = "reset_password"


# ---- 用户 ----


def create_user(session, *, user_id: str, email: str, password_hash: str,
                display_name: str, now: datetime) -> None:
    session.add(UserRow(
        id=user_id, email=email, password_hash=password_hash, display_name=display_name,
        role=ROLE_USER, status=STATUS_PENDING_EMAIL, auth_version=1,
        email_verified_at=None, reviewed_at=None, reviewed_by=None, review_note="",
        created_at=now, updated_at=now, last_login_at=None,
    ))


def get_user_by_email(session, email: str) -> UserRow | None:
    return session.execute(select(UserRow).where(UserRow.email == email)).scalar_one_or_none()


def get_user_by_id(session, user_id: str) -> UserRow | None:
    return session.get(UserRow, user_id)


def load_principal(session, *, user_id: str, session_id: str):
    """一次查询取出 (用户, 会话):访问令牌每次都要回库核对这两者(见 deps.py)。

    合成一条 JOIN 查询而不是两次往返 —— 每个已认证请求都要走这条路。
    """
    return session.execute(
        select(UserRow, AuthSessionRow)
        .join(AuthSessionRow, AuthSessionRow.user_id == UserRow.id)
        .where(UserRow.id == user_id, AuthSessionRow.id == session_id)
    ).one_or_none()


def mark_email_verified(session, user_id: str, now: datetime) -> bool:
    """邮箱验证:只在「还没验证过」时生效(原子)。返回是否真的改了。"""
    res = session.execute(
        update(UserRow)
        .where(UserRow.id == user_id, UserRow.email_verified_at.is_(None))
        .values(email_verified_at=now, status=STATUS_PENDING_APPROVAL, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def set_review(session, user_id: str, *, admin_id: str | None, approve: bool,
               note: str, now: datetime) -> int:
    """管理员审批:原子地在可审状态上落结果。返回受影响行数(0 = 状态已变,调用方重读)。

    admin_id=None 表示「不是某个管理员点出来的」(目前只有命令行建首个管理员这一处):
    那种情况 reviewed_at 照记、reviewed_by 留空、备注写明来源。
    """
    res = session.execute(
        update(UserRow)
        .where(UserRow.id == user_id, UserRow.status.in_(REVIEWABLE_STATUSES))
        .values(status=STATUS_ACTIVE if approve else STATUS_REJECTED,
                reviewed_at=now, reviewed_by=admin_id, review_note=(note or "")[:255],
                updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount)


def set_user_status(session, user_id: str, *, status: str, note: str, now: datetime) -> bool:
    """禁用 / 重新启用(带条件:只在当前状态合法时生效)。返回是否真的改了。

    禁用与启用都**不改** auth_version:禁用另有撤销全部会话这一步(service 负责);
    启用后用户直接重新登录即可。
    """
    allowed_from = (STATUS_ACTIVE,) if status == STATUS_DISABLED else (STATUS_DISABLED,)
    res = session.execute(
        update(UserRow)
        .where(UserRow.id == user_id, UserRow.status.in_(allowed_from))
        .values(status=status, review_note=(note or "")[:255] or UserRow.review_note, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def set_user_role(session, user_id: str, *, role: str, now: datetime) -> bool:
    res = session.execute(
        update(UserRow).where(UserRow.id == user_id)
        .values(role=role, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def touch_last_login(session, user_id: str, now: datetime) -> None:
    session.execute(
        update(UserRow).where(UserRow.id == user_id)
        .values(last_login_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )


def update_password(session, user_id: str, *, password_hash: str, now: datetime) -> None:
    """改密 / 重置:换哈希 + auth_version += 1(旧 access token 立即失效)+ 更新时间。"""
    session.execute(
        update(UserRow)
        .where(UserRow.id == user_id)
        .values(password_hash=password_hash,
                auth_version=UserRow.auth_version + 1,
                updated_at=now)
        .execution_options(synchronize_session=False)
    )


def update_password_hash_only(session, user_id: str, *, password_hash: str, now: datetime) -> None:
    """只换哈希、不动 auth_version:登录成功后的「参数升级重哈希」专用。

    这不是凭据变更(密码本身没变),不该把别人踢下线 —— 所以与 update_password 分开。
    """
    session.execute(
        update(UserRow).where(UserRow.id == user_id)
        .values(password_hash=password_hash, updated_at=now)
        .execution_options(synchronize_session=False)
    )


def bump_auth_version(session, user_id: str, now: datetime) -> None:
    """只递增令牌版本(登出全部设备 / 管理员强制下线):在途 access token 立即失效。"""
    session.execute(
        update(UserRow).where(UserRow.id == user_id)
        .values(auth_version=UserRow.auth_version + 1, updated_at=now)
        .execution_options(synchronize_session=False)
    )


def list_users(session, *, status: str | None = None, q: str = "",
               offset: int = 0, limit: int = 50) -> tuple[list[UserRow], int]:
    """分页列用户(先到先审:created_at 升序)。总数与页一起返回,界面要显示「共 N 人」。"""
    where = []
    if status:
        where.append(UserRow.status == status)
    if q:
        pattern = q  # contains(..., autoescape=True) 会转义 % 与 _
        where.append(or_(UserRow.email.contains(pattern, autoescape=True),
                         UserRow.display_name.contains(pattern, autoescape=True)))
    total = session.execute(select(func.count()).select_from(UserRow).where(*where)).scalar_one()
    rows = session.execute(
        select(UserRow).where(*where)
        .order_by(UserRow.created_at.asc(), UserRow.id.asc())
        .offset(max(0, offset)).limit(max(1, min(200, limit)))
    ).scalars().all()
    return list(rows), int(total)


def count_users_by_status(session) -> dict[str, int]:
    rows = session.execute(
        select(UserRow.status, func.count()).group_by(UserRow.status)
    ).all()
    return {status: int(count) for status, count in rows}


# ---- 登录会话 ----


def create_session(session, *, session_id: str, user_id: str, user_agent: str,
                   ip: str, now: datetime, expires_at: datetime) -> None:
    session.add(AuthSessionRow(
        id=session_id, user_id=user_id, user_agent=(user_agent or "")[:255], ip=(ip or "")[:45],
        created_at=now, last_used_at=now, expires_at=expires_at,
        revoked_at=None, revoked_reason="",
    ))


def get_session(session, session_id: str) -> AuthSessionRow | None:
    return session.get(AuthSessionRow, session_id)


def touch_session(session, session_id: str, now: datetime) -> None:
    session.execute(
        update(AuthSessionRow).where(AuthSessionRow.id == session_id)
        .values(last_used_at=now)
        .execution_options(synchronize_session=False)
    )


def revoke_session(session, *, session_id: str, reason: str, now: datetime) -> bool:
    """撤销单个会话(带条件:只撤还没撤销的)。返回是否真的撤了。"""
    res = session.execute(
        update(AuthSessionRow)
        .where(AuthSessionRow.id == session_id, AuthSessionRow.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=(reason or "")[:32])
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def revoke_all_sessions(session, *, user_id: str, reason: str, now: datetime,
                        except_session_id: str | None = None) -> int:
    """撤销某用户全部有效会话(登出所有设备 / 改密 / 重置 / 管理员禁用)。"""
    where = [AuthSessionRow.user_id == user_id, AuthSessionRow.revoked_at.is_(None)]
    if except_session_id:
        where.append(AuthSessionRow.id != except_session_id)
    res = session.execute(
        update(AuthSessionRow).where(*where)
        .values(revoked_at=now, revoked_reason=(reason or "")[:32])
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount)


def list_sessions(session, *, user_id: str, now: datetime) -> list[AuthSessionRow]:
    return list(session.execute(
        select(AuthSessionRow)
        .where(AuthSessionRow.user_id == user_id,
               AuthSessionRow.revoked_at.is_(None),
               AuthSessionRow.expires_at > now)
        .order_by(AuthSessionRow.created_at.desc())
    ).scalars().all())


def purge_sessions(session, *, now: datetime, keep_days: int = 30) -> int:
    """清理:删掉早就过期的会话行与它们名下的刷新令牌(审计靠 auth_audit,不靠这些行)。"""
    cutoff = now - timedelta(days=max(1, keep_days))
    stale = session.execute(
        select(AuthSessionRow.id).where(AuthSessionRow.expires_at < cutoff)
    ).scalars().all()
    if not stale:
        return 0
    session.execute(
        update(AuthRefreshTokenRow).where(AuthRefreshTokenRow.session_id.in_(stale))
        .values(consumed_at=func.coalesce(AuthRefreshTokenRow.consumed_at, now))
        .execution_options(synchronize_session=False)
    )
    session.execute(
        update(AuthSessionRow).where(AuthSessionRow.id.in_(stale))
        .values(revoked_at=func.coalesce(AuthSessionRow.revoked_at, now),
                revoked_reason="expired")
        .execution_options(synchronize_session=False)
    )
    return len(stale)


# ---- refresh 令牌链 ----


def add_refresh_token(session, *, token_id: str, session_id: str, user_id: str,
                      token_hash: str, now: datetime) -> None:
    session.add(AuthRefreshTokenRow(
        id=token_id, session_id=session_id, user_id=user_id,
        token_hash=token_hash, created_at=now, consumed_at=None,
    ))


def find_refresh_token(session, token_hash: str, *, for_update: bool = False):
    """按摘要查令牌行。for_update = 行锁:多 worker 并发刷新时串行判定,谁消费谁负责。"""
    stmt = select(AuthRefreshTokenRow).where(AuthRefreshTokenRow.token_hash == token_hash)
    if for_update:
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def consume_refresh_token(session, *, token_id: str, now: datetime) -> bool:
    """原子消费:只有把 consumed_at 从 NULL 改成时间的那一次算数(返回 True)。"""
    res = session.execute(
        update(AuthRefreshTokenRow)
        .where(AuthRefreshTokenRow.id == token_id, AuthRefreshTokenRow.consumed_at.is_(None))
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def revoke_refresh_tokens(session, *, session_id: str, now: datetime) -> int:
    """把某会话名下所有未消费的刷新令牌一次性作废(撤销会话时调用)。"""
    res = session.execute(
        update(AuthRefreshTokenRow)
        .where(AuthRefreshTokenRow.session_id == session_id,
               AuthRefreshTokenRow.consumed_at.is_(None))
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount)


# ---- 邮箱令牌 ----


def add_email_token(session, *, token_id: str, user_id: str, purpose: str, token_hash: str,
                    now: datetime, expires_at: datetime, ip: str) -> None:
    session.add(EmailTokenRow(
        id=token_id, user_id=user_id, purpose=purpose, token_hash=token_hash,
        created_at=now, expires_at=expires_at, consumed_at=None, ip=(ip or "")[:45],
    ))


def find_email_token(session, *, token_hash: str, purpose: str, for_update: bool = False):
    stmt = select(EmailTokenRow).where(EmailTokenRow.token_hash == token_hash,
                                       EmailTokenRow.purpose == purpose)
    if for_update:
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def consume_email_token(session, *, token_id: str, now: datetime) -> bool:
    """原子消费(一次性):并发点两次链接时只有一次为真。"""
    res = session.execute(
        update(EmailTokenRow)
        .where(EmailTokenRow.id == token_id, EmailTokenRow.consumed_at.is_(None))
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def consume_outstanding_email_tokens(session, *, user_id: str, purpose: str, now: datetime) -> int:
    """作废同一用途下所有还没用的令牌(新令牌签发时调用:旧链接随之失效)。"""
    res = session.execute(
        update(EmailTokenRow)
        .where(EmailTokenRow.user_id == user_id, EmailTokenRow.purpose == purpose,
               EmailTokenRow.consumed_at.is_(None))
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount)


def last_email_token_at(session, *, user_id: str, purpose: str) -> datetime | None:
    """同一用途最近一次签发时间(重发节流用)。"""
    return session.execute(
        select(func.max(EmailTokenRow.created_at))
        .where(EmailTokenRow.user_id == user_id, EmailTokenRow.purpose == purpose)
    ).scalar_one_or_none()


# ---- 会话目录 ----


def create_conversation(session, *, conv_id: str, user_id: str, thread_id: str,
                        title: str, now: datetime) -> None:
    session.add(ConversationRow(
        id=conv_id, user_id=user_id, thread_id=thread_id, title=(title or "")[:100],
        status=CONV_ACTIVE, created_at=now, updated_at=now, deleted_at=None,
    ))


def get_conversation(session, conv_id: str) -> ConversationRow | None:
    return session.get(ConversationRow, conv_id)


def get_conversation_by_thread_id(session, thread_id: str) -> ConversationRow | None:
    """按线程 id 找目录行(客户端手里只有线程 id)。走 uk_conversations_thread 唯一键。"""
    return session.execute(
        select(ConversationRow).where(ConversationRow.thread_id == thread_id)
    ).scalar_one_or_none()


def list_conversations(session, *, user_id: str, offset: int = 0, limit: int = 30,
                       status: str = CONV_ACTIVE) -> tuple[list[ConversationRow], int]:
    where = [ConversationRow.user_id == user_id, ConversationRow.status == status]
    total = session.execute(
        select(func.count()).select_from(ConversationRow).where(*where)
    ).scalar_one()
    rows = session.execute(
        select(ConversationRow).where(*where)
        .order_by(ConversationRow.updated_at.desc(), ConversationRow.id.desc())
        .offset(max(0, offset)).limit(max(1, min(200, limit)))
    ).scalars().all()
    return list(rows), int(total)


def touch_conversation(session, *, conv_id: str, now: datetime, title: str | None = None) -> bool:
    """更新会话活跃时间(以及可选的标题)。只动 active 行:已删除的会话不得被在途请求复活。"""
    values = {"updated_at": now}
    if title:
        values["title"] = title[:100]
    res = session.execute(
        update(ConversationRow)
        .where(ConversationRow.id == conv_id, ConversationRow.status == CONV_ACTIVE)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def begin_delete(session, *, conv_id: str, user_id: str,
                 now: datetime) -> tuple[str, str] | None:
    """删除第一步:原子地把 active → deleting。

    返回 (归属用户 id, 当前状态);会话不存在或不属于该用户时返回 None(调用方 404,
    绝不「顺手建一个」)。并发重复删除时只有一个请求能把状态从 active 推到 deleting。
    """
    row = session.get(ConversationRow, conv_id)
    if row is None or row.user_id != user_id:
        return None
    if row.status == CONV_ACTIVE:
        session.execute(
            update(ConversationRow)
            .where(ConversationRow.id == conv_id, ConversationRow.status == CONV_ACTIVE)
            .values(status=CONV_DELETING, updated_at=now)
            .execution_options(synchronize_session=False)
        )
    return row.user_id, (CONV_DELETING if row.status == CONV_ACTIVE else row.status)


def finish_delete(session, *, conv_id: str, now: datetime) -> bool:
    """删除第二步:deleting → deleted(墓碑)。

    已经 deleted 的返回 False(幂等:并发 / 重试的第二个请求不算失败)。
    """
    res = session.execute(
        update(ConversationRow)
        .where(ConversationRow.id == conv_id, ConversationRow.status == CONV_DELETING)
        .values(status=CONV_DELETED, deleted_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


def list_deleting(session, *, older_than: datetime) -> list[ConversationRow]:
    """卡在 deleting 的会话(进程崩在删除中途):启动对账要把它们删完。"""
    return list(session.execute(
        select(ConversationRow)
        .where(ConversationRow.status == CONV_DELETING, ConversationRow.updated_at < older_than)
        .order_by(ConversationRow.updated_at.asc())
        .limit(200)
    ).scalars().all())


# ---- 审计 ----


def add_audit(session, *, action: str, result: str, now: datetime, ip: str = "",
              actor_user_id: str | None = None, target_user_id: str | None = None,
              email: str = "", note: str = "") -> None:
    """写一条审计。只追加,不修改、不删除;调用方负责脱敏(不要塞明文令牌 / 密码)。"""
    session.add(AuthAuditRow(
        occurred_at=now, action=action[:32], result=result[:16],
        actor_user_id=actor_user_id, target_user_id=target_user_id,
        email=(email or "")[:254], ip=(ip or "")[:45], note=(note or "")[:255],
    ))


def recent_audit(session, *, limit: int = 100, target_user_id: str | None = None):
    stmt = select(AuthAuditRow).order_by(AuthAuditRow.occurred_at.desc(), AuthAuditRow.id.desc())
    if target_user_id:
        stmt = stmt.where(AuthAuditRow.target_user_id == target_user_id)
    return list(session.execute(stmt.limit(max(1, min(500, limit)))).scalars().all())
