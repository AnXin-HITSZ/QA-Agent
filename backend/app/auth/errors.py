"""认证模块的异常类型(集中定义,避免各处 import 循环)。

分类原则:
- NotConfigured 类 = 部署问题(缺密钥 / 缺数据库 / 缺发信配置):转 503,日志要显眼,
  但**不**降级成匿名可用;
- Invalid* / Replayed 类 = 请求本身不可信(令牌过期 / 无效 / 重放):转 401;
- RateLimited = 限流命中:转 429(带 Retry-After);
- RateLimitUnavailable = 限流设施(Redis)不可用:转 503 —— 失败关闭,不放开。
"""

from __future__ import annotations


class AuthNotConfigured(RuntimeError):
    """关键配置缺失(数据库 / JWT 密钥 / 发信)。认证接口据此报 503,绝不匿名放行。"""


class InvalidCredentials(Exception):
    """邮箱或密码不对。对外一律同一句话,不区分「用户不存在」与「密码错误」。"""


class AccountNotUsable(Exception):
    """密码对,但账号状态不允许登录(pending_email / pending_approval / rejected / disabled)。

    只在**密码已经验证通过**之后才可能抛出 —— 不知道密码的人无法用它探测账号状态。
    """

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class InvalidToken(Exception):
    """access token / refresh token / 邮箱令牌不可用(格式、签名、过期、已消费)。"""


class TokenReplayed(InvalidToken):
    """已被轮换掉的 refresh token 又被使用:按重放处理,撤销整个会话。"""


class RateLimited(Exception):
    """触发限流。retry_after 秒后重试。"""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"请求过于频繁,请 {retry_after} 秒后重试")
        self.retry_after = retry_after


class RateLimitUnavailable(Exception):
    """限流设施不可用(Redis 没配 / 连不上)。失败关闭:拒绝这次敏感操作。"""


class InvalidInput(ValueError):
    """用户输入本身不合法(邮箱格式 / 密码策略 / 显示名):转 400,文案可直接展示。

    code 由子类覆写(前端据此把错误定位到具体输入框);**不**给 ValueError 本身注册处理器 ——
    那会把别处(JSON 解析等)的 ValueError 一并吞成 400,那种顺手错误极难排查。
    """

    code = "invalid_input"


class InvalidPassword(InvalidInput):
    """密码不符合策略(文案直接展示)。"""

    code = "invalid_password"


class InvalidDisplayName(InvalidInput):
    """昵称不合法(文案直接展示)。"""

    code = "invalid_display_name"


class UserNotFound(Exception):
    """管理员操作的目标用户不存在:转 404。"""


class InvalidUserState(Exception):
    """账号当前状态不允许这次操作(如审批一个已经 active 的账号):转 409。"""


class MailNotConfigured(RuntimeError):
    """发信未配置(provider / AK / 发信地址缺失)。"""


class MailSendFailed(RuntimeError):
    """发信调用失败(网络 / 鉴权 / 供应商返回错误码)。"""
