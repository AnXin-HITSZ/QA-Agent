"""认证底座的行为用例:邮箱规范化 / 口令 / 令牌 / 限流 / 发信 / 存储层原子性。

这一层是**纯函数与存储语义**,不经过 HTTP —— 它们错了,上面几层的用例也可能照样绿:
比如「+标签被判成同一个地址」只有在这里才看得见。全部离线:不连 Redis、不发真信、
不碰真实 OSS。
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timedelta, timezone

import pytest

from app.auth import emails, mailer, passwords, ratelimit, tokens

# 存储行用的时间戳。**必须贴着真实时钟**:decode_access_token 用的是真实 now,
# 拿一个固定过去时刻签出来的令牌会当场过期(这一点本身就是被测语义)。
NOW = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


# ---- 邮箱规范化:合并哪些、不合并哪些 ----

@pytest.mark.parametrize("raw, want", [
    ("  Zhang@Example.COM  ", "zhang@example.com"),      # 去空白 + 整体小写
    ("　a@b.com　", "a@b.com"),                  # 全角空格也算空白
    ("a.b+lab@x.com", "a.b+lab@x.com"),                  # 句点与 +标签原样保留
    ("A.B+Lab@X.com", "a.b+lab@x.com"),                  # 只改变大小写
    ("x!#$%&'*+-/=?^_`{|}~y@x.com", "x!#$%&'*+-/=?^_`{|}~y@x.com"),  # dot-atom 允许的符号
])
def test_normalize_email(raw, want):
    assert emails.normalize_email(raw) == want


def test_email_case_variants_are_one_address():
    """大小写不同 = 同一个地址(注册 / 登录 / 找回 / 唯一键四处必须一致)。"""
    assert emails.normalize_email("Zhang@Example.com") == emails.normalize_email("zhang@example.com")


def test_plus_tag_and_dots_are_not_merged():
    """绝不按提供商规则合并:a.b+lab@x.com 与 ab@x.com 是**两个**地址。"""
    four = {"a.b+lab@x.com", "ab@x.com", "a.b@x.com", "a.blab@x.com"}
    assert len({emails.normalize_email(e) for e in four}) == 4


@pytest.mark.parametrize("bad", [
    "", "   ", "no-at-sign", "a@b", "@x.com", "a@", "a@@b.com", "a b@x.com",
    ".a@x.com", "a.@x.com", "a..b@x.com", "a@-x.com", "a@x-.com", "a@x..com",
    "a@例子.com", "a@x.com\nb", "a@x.com\x00",
    "x" * 65 + "@x.com",                       # 用户名部分超 64
    "a@" + "x" * 64 + ".com",                  # 单个标签超 63
    "a@" + "x" * 250 + ".com",                 # 整体超 254
    "aü@x.com",                                # 用户名部分非 ASCII
])
def test_normalize_email_rejects(bad):
    with pytest.raises(emails.InvalidEmail):
        emails.normalize_email(bad)


def test_is_valid_email_never_raises():
    assert emails.is_valid_email("a@b.com") is True
    assert emails.is_valid_email("nope") is False
    assert emails.is_valid_email(None) is False


def test_mask_email_hides_the_local_part():
    """日志 / 审计里不许出现完整邮箱。"""
    assert emails.mask_email("zhangsan@example.com") == "z****n@example.com"
    assert emails.mask_email("ab@x.com") == "a*@x.com"
    assert emails.mask_email("not-an-email") == "***"


# ---- 口令 ----

def test_password_hash_roundtrip_and_no_plaintext():
    stored = passwords.hash_password("Lab-QA-2026!strong")
    assert stored.startswith("$argon2id$")
    assert "Lab-QA-2026!strong" not in stored
    assert passwords.verify_password(stored, "Lab-QA-2026!strong") is True
    assert passwords.verify_password(stored, "Lab-QA-2026!stronG") is False
    # 同一个口令两次哈希不同(有盐),但都能验证
    other = passwords.hash_password("Lab-QA-2026!strong")
    assert other != stored
    assert passwords.verify_password(other, "Lab-QA-2026!strong") is True


def test_password_is_normalized_to_nfc():
    """同一个「看起来一样」的密码,在 NFC / NFD 两种输入下都能登录。"""
    import unicodedata

    composed = "pässwörd-2026!"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    stored = passwords.hash_password(decomposed)
    assert passwords.verify_password(stored, composed) is True


def test_verify_password_survives_a_broken_hash():
    """哈希损坏不能变成 500(会变成探测口),按「不匹配」处理。"""
    for broken in ("", "not-a-hash", "$argon2id$garbage"):
        assert passwords.verify_password(broken, "whatever") is False


@pytest.mark.parametrize("pwd, ok", [
    ("short7!", False), ("1234567", False), ("", False), (None, False),
    ("12345678", True), ("x" * 128, True), ("x" * 129, False),
])
def test_password_policy(pwd, ok):
    assert (passwords.validate_password(pwd) is None) is ok


def test_check_password_raises_instead_of_returning():
    """check_password 是给入口用的:不合格**抛异常**。

    这条用例的存在是因为踩过坑:create_admin 当初用的是 validate_password,
    它返回错误串而不是抛 —— 忘了看返回值,弱口令就静默通过了。
    """
    from app.auth.errors import InvalidPassword

    with pytest.raises(InvalidPassword) as ei:
        passwords.check_password("short")
    assert "8" in str(ei.value)
    passwords.check_password("12345678")          # 合格:不抛


def test_password_is_not_truncated():
    """128 位之后的内容不算,但 128 位以内每一位都参与校验(不做 72 字节截断)。"""
    long_ok = "A" * 128 + "-tail"
    stored = passwords.hash_password(long_ok)
    assert passwords.verify_password(stored, long_ok) is True
    # 若实现里做了截断,下面这条会错误地通过 —— 它必须为 False
    assert passwords.verify_password(stored, "A" * 128) is False


def test_needs_rehash_and_self_check_are_safe():
    assert passwords.needs_rehash(passwords.hash_password("12345678")) is False
    assert passwords.needs_rehash("garbage") is False   # 不抛
    passwords.self_check()                              # 启动自检不抛
    passwords.dummy_verify()                            # 时序抹平不抛


# ---- 访问令牌 ----

@pytest.fixture
def jwt_settings(auth_env):
    """auth_env 已配好测试密钥;令牌用例借用同一份配置。"""
    return auth_env.settings


def test_access_token_roundtrip_has_required_claims(jwt_settings):
    issued = datetime.now(timezone.utc).replace(microsecond=0)
    token, expires = tokens.encode_access_token(user_id="u-1", session_id="s-1",
                                                auth_version=3, now=issued)
    payload = tokens.decode_access_token(token)
    assert payload["sub"] == "u-1" and payload["sid"] == "s-1" and payload["ver"] == 3
    assert payload["iss"] == jwt_settings.auth_jwt_issuer
    assert payload["aud"] == jwt_settings.auth_jwt_audience
    assert payload["typ"] == "access" and payload["jti"]
    assert payload["exp"] - payload["iat"] == jwt_settings.auth_access_token_minutes * 60
    assert expires == issued + timedelta(minutes=jwt_settings.auth_access_token_minutes)


def test_naive_now_is_read_as_utc_not_local_time(jwt_settings):
    """朴素时间按 UTC 解释。

    这条是踩出来的:本机是 UTC+8,朴素 datetime 的 .timestamp() 会按本地时区减 8 小时,
    于是签出来的令牌「发出即过期」—— 只有当机器不在 UTC 时才现形,最容易漏到生产。
    """
    aware = datetime.now(timezone.utc).replace(microsecond=0)
    naive = aware.replace(tzinfo=None)
    from_aware, _ = tokens.encode_access_token(user_id="u-1", session_id="s-1",
                                               auth_version=1, now=aware)
    from_naive, _ = tokens.encode_access_token(user_id="u-1", session_id="s-1",
                                               auth_version=1, now=naive)
    a, b = tokens.decode_access_token(from_aware), tokens.decode_access_token(from_naive)
    assert a["iat"] == b["iat"] and a["exp"] == b["exp"]
    assert b["exp"] > datetime.now(timezone.utc).timestamp()      # 还活着,不是已经过期


def test_expired_token_is_rejected(jwt_settings):
    """过期令牌 → InvalidToken(用「过去时刻签发」造,因为 PyJWT 3 不接受 now=)。"""
    old = datetime.now(timezone.utc) - timedelta(days=1)
    token, expires = tokens.encode_access_token(user_id="u-1", session_id="s-1",
                                                auth_version=1, now=old)
    assert expires < datetime.now(timezone.utc)
    with pytest.raises(tokens.InvalidToken):
        tokens.decode_access_token(token)


def test_tampered_token_is_rejected(jwt_settings):
    token, _ = tokens.encode_access_token(user_id="u-1", session_id="s-1", auth_version=1)
    head, payload, sig = token.split(".")
    # 改 payload(把 subject 换个人)—— 签名立刻对不上
    forged = f"{head}.{payload[:-2]}XY.{sig}"
    with pytest.raises(tokens.InvalidToken):
        tokens.decode_access_token(forged)
    with pytest.raises(tokens.InvalidToken):
        tokens.decode_access_token(token + "x")


def test_alg_none_forgery_is_rejected(jwt_settings):
    """alg 写死了:攻击者把头部改成 none / HS512 都不认。"""
    import jwt as pyjwt

    payload = {"sub": "u-1", "sid": "s-1", "ver": 1, "typ": "access",
               "iss": jwt_settings.auth_jwt_issuer, "aud": jwt_settings.auth_jwt_audience,
               "iat": int(NOW.timestamp()), "exp": int((NOW + timedelta(minutes=5)).timestamp())}
    none_token = pyjwt.encode(payload, key="", algorithm="none")
    with pytest.raises(tokens.InvalidToken):
        tokens.decode_access_token(none_token)
    # 用别的密钥签的(哪怕是「合法算法」)也一样
    wrong_key = pyjwt.encode(payload, key="another-secret-entirely-0123456789", algorithm="HS256")
    with pytest.raises(tokens.InvalidToken):
        tokens.decode_access_token(wrong_key)


def test_wrong_issuer_or_audience_is_rejected(jwt_settings, monkeypatch):
    token, _ = tokens.encode_access_token(user_id="u-1", session_id="s-1", auth_version=1)
    monkeypatch.setattr(jwt_settings, "auth_jwt_audience", "someone-else")
    with pytest.raises(tokens.InvalidToken):
        tokens.decode_access_token(token)


def test_token_without_sid_is_rejected(jwt_settings):
    """必备 claim 缺失 → InvalidToken(手签一个缺 sid 的,模拟构造的令牌)。"""
    import jwt as pyjwt

    payload = {"sub": "u-1", "ver": 1, "typ": "access",
               "iss": jwt_settings.auth_jwt_issuer, "aud": jwt_settings.auth_jwt_audience,
               "iat": int(NOW.timestamp()), "exp": int((NOW + timedelta(minutes=5)).timestamp())}
    signed = pyjwt.encode(payload, tokens.jwt_secret(), algorithm="HS256")
    with pytest.raises(tokens.InvalidToken):
        tokens.decode_access_token(signed)


def test_refresh_token_type_is_not_accepted_as_access(jwt_settings):
    token, _ = tokens.encode_access_token(user_id="u-1", session_id="s-1", auth_version=1)
    assert tokens.TOKEN_TYPE == "access"
    assert tokens.decode_access_token(token)["typ"] == tokens.TOKEN_TYPE


@pytest.mark.parametrize("header, want", [
    ("Bearer abc", "abc"), ("bearer abc", "abc"), ("BEARER abc", "abc"),
    ("  Bearer   abc  ", "abc"), ("abc", ""), ("Basic abc", ""), ("", ""), (None, ""),
])
def test_bearer_token_parsing(header, want):
    assert tokens.bearer_token(header) == want


def test_jwt_secret_refuses_missing_or_weak_values(monkeypatch, auth_env):
    """密钥缺失 / 过弱一律抛 —— 绝不退回一个「谁都能伪造」的默认密钥。"""
    from app.auth.errors import AuthNotConfigured

    for weak in ("", "   ", "short", "change-me", "secret"):
        monkeypatch.setattr(auth_env.settings, "auth_jwt_secret", weak)
        with pytest.raises(AuthNotConfigured):
            tokens.jwt_secret()
    monkeypatch.setattr(auth_env.settings, "auth_jwt_secret", "x" * 32)
    assert tokens.jwt_secret() == "x" * 32


def test_random_tokens_are_long_unique_and_hashed():
    seen = set()
    for _ in range(20):
        raw, digest = tokens.new_random_token()
        assert len(raw) >= 32 and digest == tokens.sha256_hex(raw)
        assert raw not in seen and digest not in seen
        seen.update({raw, digest})
    assert tokens.constant_time_equal("abc", "abc") is True
    assert tokens.constant_time_equal("abc", "abd") is False
    assert tokens.constant_time_equal("", "") is True


# ---- 限流:失败关闭 ----

@pytest.mark.anyio
async def test_memory_limiter_counts_and_recovers(monkeypatch, auth_env):
    from app.auth.errors import RateLimited

    limiter = ratelimit.MemoryLimiter()
    clock = {"now": 1000.0}
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock["now"])

    for _ in range(3):
        await limiter.hit("login:ip:1.2.3.4", limit=3, window_seconds=60)
    with pytest.raises(RateLimited) as ei:
        await limiter.hit("login:ip:1.2.3.4", limit=3, window_seconds=60)
    assert ei.value.retry_after >= 1

    clock["now"] += 61                        # 窗口过去 → 重新计数
    await limiter.hit("login:ip:1.2.3.4", limit=3, window_seconds=60)

    # 别的 key 互不影响(按 IP / 按邮箱各算各的)
    await limiter.hit("login:ip:5.6.7.8", limit=3, window_seconds=60)


@pytest.mark.anyio
async def test_redis_limiter_wraps_connection_errors(monkeypatch, auth_env):
    """Redis 抽风 → RateLimitUnavailable(上层 503),绝不放行。"""
    from app.auth.errors import RateLimitUnavailable

    class BrokenRedis:
        async def incr(self, key):
            raise ConnectionError("redis is down")

    limiter = ratelimit.RedisLimiter(BrokenRedis())
    with pytest.raises(RateLimitUnavailable):
        await limiter.hit("login:ip:1.2.3.4", limit=5)


@pytest.mark.anyio
async def test_redis_limiter_sets_expiry_on_first_hit_and_reads_ttl(auth_env):
    from app.auth.errors import RateLimited

    calls = {"incr": 0, "expire": [], "ttl": 0}

    class FakeRedis:
        async def incr(self, key):
            calls["incr"] += 1
            return calls["incr"]

        async def expire(self, key, seconds):
            calls["expire"].append(seconds)

        async def ttl(self, key):
            calls["ttl"] += 1
            return 120

    limiter = ratelimit.RedisLimiter(FakeRedis())
    await limiter.hit("k", limit=2, window_seconds=300)
    assert calls["expire"] == [300]           # 只在第一次出现时设过期
    await limiter.hit("k", limit=2, window_seconds=300)
    assert calls["expire"] == [300]
    with pytest.raises(RateLimited) as ei:
        await limiter.hit("k", limit=2, window_seconds=300)
    assert ei.value.retry_after == 120        # 用 Redis 的剩余 TTL,不瞎猜
    assert calls["ttl"] == 1


@pytest.mark.anyio
async def test_ratelimit_fails_closed_without_redis(monkeypatch, auth_env):
    """没配 Redis → 503(失败关闭);显式关掉限流开关时才放行。"""
    from app.auth.errors import RateLimitUnavailable

    ratelimit.install(None)          # 卸掉夹具装的内存实现,走真实的「按配置懒建」这条路
    monkeypatch.setattr(auth_env.settings, "redis_url", "")
    monkeypatch.setattr(auth_env.settings, "auth_rate_limit_enabled", True)
    with pytest.raises(RateLimitUnavailable):
        await ratelimit.hit("login:ip:1.2.3.4", limit=5)

    monkeypatch.setattr(auth_env.settings, "auth_rate_limit_enabled", False)
    await ratelimit.hit("login:ip:1.2.3.4", limit=5)      # 关掉开关 = 明知故犯,放行


# ---- 发信:SMTP 通道(假服务器,绝不触网)----

def _smtp_mailer(**over):
    cfg = dict(host="smtpdm.aliyuncs.com", port=465, username="noreply@mail.qa.anxin-hitsz.com",
               password="SMTP-secret", from_addr="noreply@mail.qa.anxin-hitsz.com",
               from_alias="QA-Agent 实验室助手", timeout=5.0)
    cfg.update(over)
    return mailer.SmtpMailer(**cfg)


class _FakeSMTP:
    """假 SMTP 服务器:把调用按**顺序**记下来 —— 口令只能出现在 STARTTLS 之后。"""

    def __init__(self, *, starttls_ok=True, login_error=None):
        self.events: list[tuple] = []
        self._starttls_ok = starttls_ok
        self._login_error = login_error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.events.append(("quit",))
        return False

    def starttls(self, *, context=None):
        if not self._starttls_ok:
            raise smtplib.SMTPNotSupportedError("server does not support STARTTLS")
        self.events.append(("starttls", context))
        return 220, b"ok"

    def login(self, username, password):
        self.events.append(("login", username, password))
        if self._login_error is not None:
            raise self._login_error

    def send_message(self, msg):
        self.events.append(("send", msg["To"], msg))


def test_smtp_message_is_pure_ascii_and_round_trips():
    """整封信必须是 7bit 纯 ASCII(不依赖服务器支持 8BITMIME),中文头按 RFC 2047 编码。"""
    import io
    from email import message_from_bytes
    from email.generator import BytesGenerator
    from email.header import decode_header, make_header

    body = "你好,\n请点 http://example.com/verify-email?token=abc\n链接 60 分钟内有效。"
    msg = _smtp_mailer()._message(to="u@example.com", subject="验证你的邮箱 · QA-Agent 实验室助手",
                                  text=body, html=None)
    # 与 smtplib.send_message 内部同款序列化路径(默认 compat32 生成器 + CRLF)
    buf = io.BytesIO()
    BytesGenerator(buf).flatten(msg, linesep="\r\n")
    raw = buf.getvalue()

    assert all(b < 128 for b in raw)                      # 纯 ASCII:任何服务器都收得下
    parsed = message_from_bytes(raw)
    assert parsed["To"] == "u@example.com"
    assert parsed.get_content_type() == "text/plain"
    decoded = parsed.get_payload(decode=True).decode(parsed.get_content_charset())
    assert decoded.rstrip("\n") == body                    # set_content 会补一个结尾换行
    assert str(make_header(decode_header(parsed["Subject"]))) == "验证你的邮箱 · QA-Agent 实验室助手"
    assert "实验室助手" in str(make_header(decode_header(parsed["From"])))  # 显示名保留
    assert "noreply@mail.qa.anxin-hitsz.com" in parsed["From"]
    assert "SMTP-secret" not in raw.decode("ascii")        # 口令绝不进信体 / 头


@pytest.mark.anyio
async def test_smtp_uses_implicit_tls_on_465(monkeypatch):
    """465 = 隐式 TLS:走 SMTP_SSL,不做 STARTTLS;证书校验开着。"""
    srv = _FakeSMTP()
    seen: dict = {}

    def fake_ssl(host, port, timeout=None, context=None):
        seen.update(host=host, port=port, context=context)
        return srv

    def boom(*a, **k):                                     # 明文通道不该被碰
        raise AssertionError("465 上不该用明文 SMTP")

    monkeypatch.setattr(smtplib, "SMTP_SSL", fake_ssl)
    monkeypatch.setattr(smtplib, "SMTP", boom)

    await _smtp_mailer(port=465).send(to="u@example.com", subject="s", text="t")

    assert (seen["host"], seen["port"]) == ("smtpdm.aliyuncs.com", 465)
    assert seen["context"].check_hostname is True          # 不提供「跳过证书校验」的开关
    assert [e[0] for e in srv.events] == ["login", "send", "quit"]


@pytest.mark.anyio
async def test_smtp_requires_starttls_before_credentials(monkeypatch):
    """其它端口:必须先 STARTTLS 再 login —— 顺序错了就是明文递口令。"""
    srv = _FakeSMTP()
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **k: srv)
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: srv)

    await _smtp_mailer(port=587).send(to="u@example.com", subject="s", text="t")

    kinds = [e[0] for e in srv.events]
    assert kinds == ["starttls", "login", "send", "quit"]
    assert srv.events[0][1].check_hostname is True         # STARTTLS 也带校验上下文


@pytest.mark.anyio
async def test_smtp_refuses_to_send_credentials_when_starttls_unsupported(monkeypatch):
    """服务器不支持 STARTTLS → 直接失败,**不**退化成明文登录。"""
    from app.auth.errors import MailSendFailed

    srv = _FakeSMTP(starttls_ok=False)
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **k: srv)
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: srv)

    with pytest.raises(MailSendFailed) as ei:
        await _smtp_mailer(port=25).send(to="u@example.com", subject="s", text="t")
    assert "STARTTLS" in str(ei.value)
    assert ("login", "noreply@mail.qa.anxin-hitsz.com", "SMTP-secret") not in srv.events
    assert not any(e[0] == "send" for e in srv.events)     # 一封都没发出去


@pytest.mark.anyio
async def test_smtp_maps_transport_and_auth_errors_without_leaking_password(monkeypatch):
    from app.auth.errors import MailSendFailed

    # 1) 连不上 / TLS 失败:OSError 系
    def broken(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP_SSL", broken)
    with pytest.raises(MailSendFailed) as ei:
        await _smtp_mailer().send(to="u@example.com", subject="s", text="t")
    assert "OSError" in str(ei.value) and "SMTP-secret" not in str(ei.value)

    # 2) 认证失败:服务器说的话要带出来,便于排查(口令本身不会出现在里面)
    srv = _FakeSMTP(login_error=smtplib.SMTPAuthenticationError(535, b"auth failed"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **k: srv)
    with pytest.raises(MailSendFailed) as ei:
        await _smtp_mailer().send(to="u@example.com", subject="s", text="t")
    assert "SMTPAuthenticationError" in str(ei.value)
    assert "SMTP-secret" not in str(ei.value)


def test_fake_mailer_logs_links_only_outside_production(monkeypatch, auth_env, caplog):
    """fake 模式把链接打进日志,方便本地演练;生产环境(/未知 env)一律不打。"""
    import logging

    from app.auth import service as svc_mod

    fake = mailer.FakeMailer()
    with caplog.at_level(logging.INFO):
        monkeypatch.setattr(auth_env.settings, "app_env", "dev")
        svc_mod.AuthService._log_dev_link(auth_env.service, fake, kind="验证",
                                          to="u@example.com", link="http://x/v?token=abc")
        assert "http://x/v?token=abc" in caplog.text

        caplog.clear()
        monkeypatch.setattr(auth_env.settings, "app_env", "prod")   # 生产:令牌不进日志
        svc_mod.AuthService._log_dev_link(auth_env.service, fake, kind="验证",
                                          to="u@example.com", link="http://x/v?token=abc")
        assert "token=abc" not in caplog.text

        caplog.clear()
        monkeypatch.setattr(auth_env.settings, "app_env", "whatever")  # 没写清 = 按生产对待
        svc_mod.AuthService._log_dev_link(auth_env.service, fake, kind="验证",
                                          to="u@example.com", link="http://x/v?token=abc")
        assert "token=abc" not in caplog.text

        caplog.clear()
        monkeypatch.setattr(auth_env.settings, "app_env", "dev")
        svc_mod.AuthService._log_dev_link(auth_env.service, _smtp_mailer(), kind="验证",
                                          to="u@example.com", link="http://x/v?token=abc")
        assert "token=abc" not in caplog.text      # 真发信通道:链接绝不进日志


def test_mailer_must_be_configured_explicitly(monkeypatch, auth_env):
    """未配置发信 → MailNotConfigured(503);**绝不**降级成「把链接回显给客户端」。"""
    from app.auth.errors import MailNotConfigured

    mailer.install_mailer(None)
    monkeypatch.setattr(auth_env.settings, "mail_provider", "")
    with pytest.raises(MailNotConfigured):
        mailer.get_mailer()

    monkeypatch.setattr(auth_env.settings, "mail_provider", "sendgrid")
    with pytest.raises(MailNotConfigured):
        mailer.get_mailer()

    # SMTP 通道:缺项要逐个点名(半套 SMTP 配置不许安静地不发信)
    monkeypatch.setattr(auth_env.settings, "mail_provider", "smtp")
    monkeypatch.setattr(auth_env.settings, "mail_smtp_host", "")
    monkeypatch.setattr(auth_env.settings, "mail_smtp_username", "noreply@mail.x.com")
    monkeypatch.setattr(auth_env.settings, "mail_smtp_password", "pw")
    monkeypatch.setattr(auth_env.settings, "mail_smtp_from", "noreply@mail.x.com")
    with pytest.raises(MailNotConfigured) as ei:
        mailer.get_mailer()
    assert "MAIL_SMTP_HOST" in str(ei.value)

    monkeypatch.setattr(auth_env.settings, "mail_smtp_host", "smtpdm.aliyuncs.com")
    kind = mailer.get_mailer()
    assert isinstance(kind, mailer.SmtpMailer)
    assert (kind.host, kind.port) == ("smtpdm.aliyuncs.com", 465)
    assert kind.from_alias == auth_env.settings.mail_from_alias   # 显示名与发信地址分开配

    # 换 provider 前先卸掉已构造的实例:get_mailer 会缓存(装一次用到底,这与运行期一致)
    mailer.install_mailer(None)
    monkeypatch.setattr(auth_env.settings, "mail_provider", "fake")
    assert isinstance(mailer.get_mailer(), mailer.FakeMailer)
    mailer.install_mailer(None)


def test_comment_like_env_values_are_named_at_startup(caplog):
    """`.env` 里 `KEY=  # 说明` 解析出来的值就是注释文本(实测)—— 启动时点名它,不静默清洗。"""
    import logging

    from app.config import Settings

    with caplog.at_level(logging.WARNING):
        s = Settings(_env_file=None, mail_provider="# smtp / fake",
                     auth_cookie_domain="# 留空 = 跟随请求主机")
    assert "mail_provider" in caplog.text and "auth_cookie_domain" in caplog.text
    assert s.mail_provider.startswith("#")     # 值原样保留:改不改是人的事,报不报是代码的事


# ---- 存储层的原子语义(HTTP 用例表达不出来的那部分)----

def _mk_user(session, email="u@example.com"):
    from app.auth import store

    store.create_user(session, user_id=tokens.new_id(), email=email,
                      password_hash="x", display_name="测试", now=NOW)
    session.flush()      # 会话是 autoflush=False:不 flush 的话下面就查不到刚插入的行
    return store.get_user_by_email(session, email)


def test_email_token_consume_is_atomic_and_single_use(auth_env):
    """两条并发请求同时点同一个链接:只有一条算数(consume 是带条件的 UPDATE)。"""
    from app.auth import store

    with auth_env.db.session_scope() as session:
        user = _mk_user(session)
        raw, digest = tokens.new_random_token()
        store.add_email_token(session, token_id=tokens.new_id(), user_id=user.id,
                              purpose="verify_email", token_hash=digest, now=NOW,
                              expires_at=NOW + timedelta(hours=1), ip="1.2.3.4")
        session.flush()
        row = store.find_email_token(session, token_hash=digest, purpose="verify_email")
        assert store.consume_email_token(session, token_id=row.id, now=NOW) is True
        assert store.consume_email_token(session, token_id=row.id, now=NOW) is False


def test_email_tokens_are_isolated_by_purpose(auth_env):
    """验证邮箱的令牌不能拿去重置密码(按 purpose 查)。"""
    from app.auth import store

    with auth_env.db.session_scope() as session:
        user = _mk_user(session)
        raw, digest = tokens.new_random_token()
        store.add_email_token(session, token_id=tokens.new_id(), user_id=user.id,
                              purpose="verify_email", token_hash=digest, now=NOW,
                              expires_at=NOW + timedelta(hours=1), ip="")
        session.flush()
        assert store.find_email_token(session, token_hash=digest, purpose="verify_email")
        assert store.find_email_token(session, token_hash=digest, purpose="reset_password") is None


def test_issuing_a_new_token_invalidates_the_old_link(auth_env):
    from app.auth import store

    with auth_env.db.session_scope() as session:
        user = _mk_user(session)
        _, first = tokens.new_random_token()
        store.add_email_token(session, token_id=tokens.new_id(), user_id=user.id,
                              purpose="reset_password", token_hash=first, now=NOW,
                              expires_at=NOW + timedelta(hours=1), ip="")
        session.flush()
        assert store.consume_outstanding_email_tokens(session, user_id=user.id,
                                                      purpose="reset_password", now=NOW) == 1
        old = store.find_email_token(session, token_hash=first, purpose="reset_password")
        assert old is not None and old.consumed_at is not None   # 旧链接作废


def test_email_token_hash_is_what_is_stored(auth_env):
    """库里只存摘要:拿到整张表也还原不出链接里的令牌。"""
    from app.auth import store

    with auth_env.db.session_scope() as session:
        user = _mk_user(session, email="h@example.com")
        raw, digest = tokens.new_random_token()
        store.add_email_token(session, token_id=tokens.new_id(), user_id=user.id,
                              purpose="verify_email", token_hash=digest, now=NOW,
                              expires_at=NOW + timedelta(hours=1), ip="")
        session.flush()
        stored = session.execute(
            __import__("sqlalchemy").text("SELECT token_hash FROM email_tokens")
        ).scalar_one()
        assert stored == digest and raw not in stored
        assert tokens.sha256_hex(raw) == stored


def test_set_review_is_compare_and_set(auth_env):
    """审批是 CAS:第二个管理员在已定局的账号上审批拿到 0 行(上层据此回 409)。"""
    from app.auth import store

    with auth_env.db.session_scope() as session:
        user = _mk_user(session, email="cas@example.com")
        assert store.set_review(session, user.id, admin_id=None, approve=True,
                                note="第一次", now=NOW) == 1
        assert store.set_review(session, user.id, admin_id=None, approve=False,
                                note="抢输的第二次", now=NOW) == 0


def test_email_lookup_is_exact_match_not_case_insensitive(auth_env):
    """按精确字符串查(utf8mb4_bin):大小写不同就是查不到 —— 规范化是**入口**的责任。"""
    from app.auth import store

    with auth_env.db.session_scope() as session:
        _mk_user(session, email="Mixed@Example.com")
        assert store.get_user_by_email(session, "Mixed@Example.com") is not None
        assert store.get_user_by_email(session, "mixed@example.com") is None


def test_revoke_all_sessions_is_a_two_step_contract(auth_env):
    """登出全部 = 撤销会话行 + 递增 auth_version,两步都要做,store 不替调用方合并。

    刷新链(DELETE 会话那条路会显式消费)这里不动:会话行一撤,refresh 就换不出东西了。
    这里钉住的是两张表各自的可见结果 —— 少做一步(比如只 bump 不撤会话),
    「登出全部」就只挡住 15 分钟的 access token,刷新令牌还能继续换新的。
    """
    from app.auth import store

    with auth_env.db.session_scope() as session:
        user = _mk_user(session, email="sess@example.com")
        sid = tokens.new_id()
        store.create_session(session, session_id=sid, user_id=user.id, user_agent="ua",
                             ip="1.2.3.4", now=NOW, expires_at=NOW + timedelta(days=30))
        _, digest = tokens.new_random_token()
        store.add_refresh_token(session, token_id=tokens.new_id(), session_id=sid,
                                user_id=user.id, token_hash=digest, now=NOW)
        session.flush()
        before = store.get_user_by_id(session, user.id).auth_version

        assert store.revoke_all_sessions(session, user_id=user.id, reason="logout_all",
                                        now=NOW) == 1
        store.bump_auth_version(session, user.id, NOW)
        session.flush()
        row = store.get_session(session, sid)
        assert row.revoked_at is not None and row.revoked_reason == "logout_all"
        # store 的更新都是 Core UPDATE + synchronize_session=False:已加载进本会话的
        # UserRow 不会自己变新,得显式 refresh(service 里也是这么写的,见 _review_sync)。
        session.refresh(user)
        assert user.auth_version == before + 1
        # 再撤一次已经是 0 行(幂等,不会把撤销时间覆盖成新的)
        assert store.revoke_all_sessions(session, user_id=user.id, reason="logout_all",
                                         now=NOW) == 0
