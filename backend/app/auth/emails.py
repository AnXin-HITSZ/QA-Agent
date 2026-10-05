"""邮箱规范化与校验:注册、登录、找回密码、数据库唯一键四处**必须**用同一个函数。

规则(技术方案 §4,逐条明确,不做任何「聪明」的合并):
1. 去掉首尾空白(含全角空格等 Unicode 空白);
2. 整体转小写 —— 域名大小写不敏感是共识;用户名部分我们也统一小写,规则简单且
   四处一致(不区分大小写的匹配)。注意:这**不是**按某家邮件提供商的规则合并地址;
3. **保留**用户名部分的句点与 +标签:a.b+lab@x.com 与 ab@x.com 是两个人,绝不合并;
4. 不认 IDN / 不做别名展开,域名按字面校验(LDH:字母数字与连字符);
5. 唯一键与查询都按**精确字符串**判定:users.email 列显式 COLLATE utf8mb4_bin
   (库默认的 utf8mb4_0900_ai_ci 不区分重音,会把 é / e 判成同一个地址而意外合并)。
6. 不把邮箱当主键:主键是 UUID,邮箱日后可改(改邮箱不在本期范围,但结构上不挡)。

校验只做「格式上确实不像邮箱」的排除,不试图完整实现 RFC 5322 —— 真正确认邮箱有效性的
是验证邮件那一步。
"""

from __future__ import annotations

from app.auth.errors import InvalidInput

MAX_EMAIL_LENGTH = 254          # RFC 5321 的路径上限
MAX_LOCAL_LENGTH = 64

# 用户名部分允许的字符(未加引号的 dot-atom 子集):常见可打印 ASCII,不含空格与括号等。
_LOCAL_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+-/=?^_`{|}~.")


class InvalidEmail(InvalidInput):
    """邮箱格式不合法(文案可直接展示给用户)。"""

    code = "invalid_email"


def normalize_email(raw: str | None) -> str:
    """规范化并校验;不合法抛 InvalidEmail。返回值即入库 / 比较 / 查询用的那个字符串。"""
    email = (raw or "").strip()
    if not email:
        raise InvalidEmail("邮箱不能为空")
    if len(email) > MAX_EMAIL_LENGTH:
        raise InvalidEmail(f"邮箱过长(最多 {MAX_EMAIL_LENGTH} 个字符)")
    if any(ch.isspace() for ch in email):
        raise InvalidEmail("邮箱中不能包含空白字符")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in email):
        raise InvalidEmail("邮箱中不能包含控制字符")

    local, at, domain = email.rpartition("@")
    if not at or not local or not domain:
        raise InvalidEmail("邮箱格式不正确,应形如 name@example.com")
    if len(local) > MAX_LOCAL_LENGTH:
        raise InvalidEmail(f"邮箱用户名部分过长(最多 {MAX_LOCAL_LENGTH} 个字符)")
    if local.startswith(".") or local.endswith(".") or ".." in local:
        raise InvalidEmail("邮箱格式不正确(用户名部分的句点位置不对)")
    bad = sorted({ch for ch in local.lower() if ch not in _LOCAL_ALLOWED})
    if bad:
        raise InvalidEmail(f"邮箱用户名部分包含不支持的字符:{' '.join(bad)}")

    labels = domain.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        raise InvalidEmail("邮箱域名不正确(应形如 example.com)")
    for label in labels:
        if len(label) > 63 or label.startswith("-") or label.endswith("-"):
            raise InvalidEmail("邮箱域名不正确")
        if not all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in label):
            raise InvalidEmail("邮箱域名包含不支持的字符(暂不支持中文域名)")

    # 整体小写:域名部分是本意,用户名部分一并小写以保证「注册 = 登录 = 找回 = 唯一键」四处的
    # 比较规则完全一致(见模块 docstring)。句点与 +标签原样保留。
    return f"{local}@{domain}".lower()


def is_valid_email(raw: str | None) -> bool:
    """只判断格式,不抛异常(给「静默接受」的接口用,如找回密码)。"""
    try:
        normalize_email(raw)
        return True
    except InvalidEmail:
        return False


def mask_email(email: str) -> str:
    """脱敏展示(日志 / 审计里用):a****z@example.com。绝不把完整邮箱写进日志。"""
    local, at, domain = (email or "").rpartition("@")
    if not at:
        return "***"
    if len(local) <= 2:
        shown = local[:1] + "*"
    else:
        shown = f"{local[0]}****{local[-1]}"
    return f"{shown}@{domain}"
