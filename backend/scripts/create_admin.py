"""建立第一个管理员 —— 只能从命令行做,系统里没有第二个入口。

用法(在 backend 目录、用跑服务的那个 Python):

    python scripts/create_admin.py --email admin@example.com --name 张老师
    # 密码由终端交互输入(不回显);非交互场景(自动化)用:
    python scripts/create_admin.py --email admin@example.com --password-stdin

为什么长这样(技术方案 §3.3):
- 公共注册接口只发 role=user,压根没有「注册成为管理员」的路径;所以**能登上服务器的人**
  才建得出第一个管理员 —— 这正是我们想要的信任边界;
- 密码**不走命令行参数**:`--password xxx` 会留在 shell 历史、进程列表、以及
  systemd / bash 的日志里;
- 密码同样过一遍 check_password:管理员账号没有弱口令豁免;
- 邮箱视为已验证(操作者当面确认过这个邮箱是他的):否则首次登录会卡在「需要先收验证邮件」,
  而这时发信配置可能还没准备好;
- 一律记审计(bootstrap_admin),谁在什么时候建了哪个管理员,查得到;
- 已存在同名邮箱时**拒绝执行**,不改密码、不覆盖 —— 改密码是另一件事(reset_password),
  不要用一个「建管理员」的脚本顺手完成,免得手滑把线上管理员的口令悄悄换掉。

跑之前要配好 METERING_MYSQL_URL 且已执行 0003 迁移(见 docs/认证鉴权与用户管理技术方案.md §9)。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime, timezone
from pathlib import Path

# 允许 `python scripts/create_admin.py` 直接跑(此时 sys.path[0] 是 scripts/,不含 backend/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import db, store  # noqa: E402
from app.auth.emails import InvalidEmail, normalize_email  # noqa: E402
from app.auth.errors import AuthNotConfigured  # noqa: E402
from app.auth.errors import InvalidPassword  # noqa: E402
from app.auth.passwords import check_password, hash_password  # noqa: E402
from app.auth.service import _mask  # noqa: E402
from app.auth.tokens import new_id  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ALREADY_EXISTS = 3
EXIT_NOT_CONFIGURED = 4


def create_admin(*, email: str, password: str, display_name: str = "") -> str:
    """建一个 active 的管理员,返回 user_id。邮箱已存在时抛 RuntimeError(不改动任何行)。

    与命令行解耦,便于测试直接调(测试里它跑在临时 SQLite 上)。
    """
    norm = normalize_email(email)
    check_password(password)                         # 管理员也没有弱口令豁免(不合格直接抛)
    name = (display_name or "").strip() or norm.split("@", 1)[0]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    user_id = new_id()

    with db.session_scope() as session:
        if store.get_user_by_email(session, norm) is not None:
            raise RuntimeError(f"已存在该邮箱的账号:{_mask(norm)}(本脚本不改动已有账号)")
        store.create_user(session, user_id=user_id, email=norm,
                          password_hash=hash_password(password), display_name=name[:64], now=now)
        # 先 flush:下面三步都是「带条件的 UPDATE」(CAS),它们直接打在数据库上。会话是
        # autoflush=False 的,不先落 INSERT 的话这些 UPDATE 会匹配 0 行、静默什么都不做
        # (用户就停在 pending_email,登不进去)。
        session.flush()
        # 走一遍真实状态机(pending_email → pending_approval → active),而不是直接改 status:
        # 这样「命令行的账号也得是已验证 + 已被批准」这条不变量与线上完全一致。
        # 每一步都检查返回值 —— CAS 更新匹配 0 行时是静默不生效的,不检查就会建出一个
        # 停在 pending_email、永远登不进去的账号(这正是先 flush 那个坑的教训)。
        for ok, what in (
            (store.mark_email_verified(session, user_id, now), "邮箱标记为已验证"),
            (store.set_review(session, user_id, admin_id=None, approve=True, now=now,
                              note="首个管理员,命令行建立(未经管理员审批)"), "审批通过"),
            (store.set_user_role(session, user_id, role=store.ROLE_ADMIN, now=now), "设为 admin"),
        ):
            if not ok:
                raise RuntimeError(f"建立管理员失败:{what} 未生效(数据库状态与预期不符)")
        store.add_audit(session, action="bootstrap_admin", result="ok", now=now,
                        target_user_id=user_id, email=norm, note="命令行建立首个管理员")
    return user_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建立第一个管理员账号(仅命令行)")
    parser.add_argument("--email", required=True, help="管理员邮箱")
    parser.add_argument("--name", default="", help="显示名;留空取邮箱 @ 前部分")
    parser.add_argument("--password-stdin", action="store_true",
                        help="从标准输入读密码(自动化用);默认交互输入")
    args = parser.parse_args(argv)

    if not db.configured():
        print("错误:未配置 METERING_MYSQL_URL —— 用户表在 MySQL 里,先按技术方案 §10 配置。",
              file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    password = (sys.stdin.readline() if args.password_stdin else getpass.getpass("请输入管理员密码: ")).rstrip("\r\n")
    if not args.password_stdin:
        confirm = getpass.getpass("请再输入一次: ")
        if password != confirm:
            print("错误:两次输入的密码不一致。", file=sys.stderr)
            return EXIT_USAGE

    try:
        user_id = create_admin(email=args.email, password=password, display_name=args.name)
    except (InvalidEmail, InvalidPassword) as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return EXIT_USAGE
    except RuntimeError as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return EXIT_ALREADY_EXISTS
    except AuthNotConfigured as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    print(f"已建立管理员:{_mask(args.email)} (user_id={user_id})")
    print("请立即用该账号登录并核对「我的账号」页;系统里不会保存这个密码的明文。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
