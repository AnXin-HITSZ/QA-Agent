"""发信:阿里云邮件推送的 SMTP 通道,外加测试 / 本地用的 fake。

设计:
- 传输层只负责「把一封信发出去」,不含任何业务文案(模板在 service.py);
- provider 未配置时抛 MailNotConfigured —— 注册 / 找回密码接口据此报 503 并写明缺什么,
  **不**假装发过信、也不把令牌回显给客户端(那是极危险的降级);
- provider=fake 把邮件留在内存里(mailer.sent),只给测试与本地演练用;
- SMTP 这条路**没有**在本项目里连过真实服务:第一次上线前必须用一封真邮件验证
  (技术方案 §11 的未验证项)。
"""

from __future__ import annotations

import functools
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

import anyio

from app.auth.errors import MailNotConfigured, MailSendFailed
from app.config import get_settings

logger = logging.getLogger(__name__)


class Mailer:
    """发信接口(测试替身按同一形状实现)。"""

    async def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        raise NotImplementedError


class FakeMailer(Mailer):
    """把邮件记在内存里:测试与本地演练用,绝不触网。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})

    def last_link(self) -> str:
        """取最近一封邮件里的第一个链接(测试断言验证 / 重置链接用)。"""
        import re

        for message in reversed(self.sent):
            m = re.search(r"https?://\S+", message["text"])
            if m:
                return m.group(0).rstrip("。，,)")
        raise AssertionError("最近这些邮件里没有链接")


class SmtpMailer(Mailer):
    """SMTP 通道(阿里云邮件推送的 smtpdm.aliyuncs.com 等)。

    传输安全策略:
    - 465 = 隐式 TLS(SMTP_SSL);其它端口必须 STARTTLS —— 服务器不支持就直接判失败,
      **绝不**把账号口令明文递出去;
    - 证书走 ssl.create_default_context():校验证书与主机名,不提供「跳过验证」开关;
    - 一封一连接:验证 / 重置邮件量小,不做连接池。smtplib 是阻塞 IO,整体丢线程池跑
      (与 db.run 同一个约定:异步路由里不让阻塞调用占住事件循环)。
    """

    def __init__(self, *, host: str, port: int, username: str, password: str, from_addr: str,
                 from_alias: str = "", timeout: float = 10.0) -> None:
        self.host = (host or "").strip()
        self.port = int(port or 0)
        self.username = username
        self.password = password
        self.from_addr = (from_addr or "").strip()
        self.from_alias = (from_alias or "").strip()
        self.timeout = max(1.0, float(timeout))

    def _message(self, *, to: str, subject: str, text: str,
                 html: str | None = None) -> EmailMessage:
        msg = EmailMessage()
        # 显示名由内容策略自己按 RFC 2047 编码(中文别名不会污染地址本身)。
        msg["From"] = formataddr((self.from_alias, self.from_addr)) if self.from_alias else self.from_addr
        msg["To"] = to
        msg["Subject"] = subject
        # 正文一律 base64:整封信变成纯 ASCII,不要求服务器支持 8BITMIME / SMTPUTF8。
        msg.set_content(text, cte="base64")
        if html:
            msg.add_alternative(html, subtype="html", cte="base64")
        return msg

    def _send_sync(self, *, to: str, subject: str, text: str, html: str | None) -> None:
        msg = self._message(to=to, subject=subject, text=text, html=html)
        context = ssl.create_default_context()
        try:
            if self.port == 465:
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                          context=context)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with server:                       # 退出时 quit();半途出错则 close(),不挂连接
                if self.port != 465:
                    try:
                        server.starttls(context=context)
                    except smtplib.SMTPNotSupportedError as exc:
                        raise MailSendFailed(
                            "SMTP 服务器不支持 STARTTLS:这个端口发口令是明文。"
                            "请改用 465(隐式 TLS),或换支持 STARTTLS 的端口。") from exc
                server.login(self.username, self.password)
                server.send_message(msg)
        except MailSendFailed:
            raise
        except (smtplib.SMTPException, OSError) as exc:
            # 异常里不会有口令(smtplib 不回显 login 凭据),只报类型与服务器说的话。
            raise MailSendFailed(f"SMTP 发信失败:{type(exc).__name__}: {exc}") from exc

    async def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        await anyio.to_thread.run_sync(
            functools.partial(self._send_sync, to=to, subject=subject, text=text, html=html))
        logger.info("发信成功(SMTP):to=%s subject=%s", _mask(to), subject)


def _mask(email: str) -> str:
    from app.auth.emails import mask_email

    return mask_email(email)


# ---- 安装 / 取用(与 metering 的 install_store 同款:测试可替换)----

_mailer: Mailer | None = None


def install_mailer(mailer: Mailer | None) -> None:
    """安装发信实现(测试替身 / 本地 fake);传 None 恢复按配置构造。"""
    global _mailer
    _mailer = mailer


def get_mailer() -> Mailer:
    """取发信实现:已安装的优先;否则按 MAIL_PROVIDER 构造;未配置抛 MailNotConfigured。"""
    global _mailer
    if _mailer is not None:
        return _mailer

    settings = get_settings()
    provider = (settings.mail_provider or "").strip().lower()
    if provider in ("", "none", "off", "disabled"):
        raise MailNotConfigured(
            "未配置发信(MAIL_PROVIDER 为空):无法发送验证 / 重置邮件。"
            "请在 backend/.env 里配置 MAIL_PROVIDER=smtp + MAIL_SMTP_HOST / MAIL_SMTP_USERNAME / "
            "MAIL_SMTP_PASSWORD / MAIL_SMTP_FROM(见 .env.example)。"
        )
    if provider == "fake":
        _mailer = FakeMailer()
        return _mailer

    if provider != "smtp":
        # 值以 # 开头 = .env 里把注释写在了等号后面(空值 + 行内注释会被当成值),给一句明确提示。
        hint = "(这个值看起来像注释:.env 里请把注释单独放一行)" if provider.startswith("#") else ""
        raise MailNotConfigured(f"不认识的 MAIL_PROVIDER:{provider}{hint}(可选 smtp / fake)")

    missing = [name for name, value in (
        ("MAIL_SMTP_HOST", settings.mail_smtp_host),
        ("MAIL_SMTP_USERNAME", settings.mail_smtp_username),
        ("MAIL_SMTP_PASSWORD", settings.mail_smtp_password),
        ("MAIL_SMTP_FROM", settings.mail_smtp_from),
    ) if not (value or "").strip()]
    if missing:
        raise MailNotConfigured(f"发信(SMTP)配置不完整,缺少:{', '.join(missing)}")
    _mailer = SmtpMailer(
        host=settings.mail_smtp_host,
        port=settings.mail_smtp_port,
        username=settings.mail_smtp_username,
        password=settings.mail_smtp_password,
        from_addr=settings.mail_smtp_from,
        from_alias=settings.mail_from_alias,
        timeout=settings.mail_timeout_seconds,
    )
    return _mailer
