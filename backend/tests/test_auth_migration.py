"""迁移 0003 与认证 / 用户管理模型的一致性(**离线**,不连任何数据库)。

与 tests/test_metering_migration.py 同一套解析与比对(共用 tests/migrationkit.py),
这里负责六张新表:users / auth_sessions / auth_refresh_tokens / email_tokens /
conversations / auth_audit。

额外看护两点:
- **邮箱列的排序规则**:唯一键必须是精确字符串比较(utf8mb4_bin)。跟随库默认的
  utf8mb4_0900_ai_ci 会把 é / e、大小写不同的地址判成同一个而意外合并账号;
- **没有无主的表**:迁移里出现的每一张表都必须归某个模型模块所有,防止两边只改一处。
"""

from __future__ import annotations

from tests import migrationkit as mig
from tests.test_metering_migration import TABLES as METERING_TABLES

AUTH_TABLES = (
    "users",
    "auth_sessions",
    "auth_refresh_tokens",
    "email_tokens",
    "conversations",
    "auth_audit",
)


def model_tables() -> dict[str, mig.Table]:
    return mig.model_tables("app.auth.tables")


def _column(table: str, name: str) -> mig.Column:
    return next(c for c in model_tables()[table].columns if c.name == name)


def test_migration_matches_auth_models():
    """0003 建的六张表必须与 app/auth/tables.py 逐项一致(列 / 类型 / 可空 / 主键 / 唯一键 / 索引)。"""
    model, migration = model_tables(), mig.migration_tables()
    missing = sorted(set(model) - set(migration))
    assert not missing, f"迁移里少了这些表:{missing}"
    for name, want in model.items():
        mig.compare_tables(migration[name], want, label=name)


def test_auth_tables_are_exactly_the_planned_ones():
    """六张表一张不多、一张不少(少了只会在线上写用户时才发现)。"""
    assert set(AUTH_TABLES) == set(model_tables())
    assert set(AUTH_TABLES) <= set(mig.migration_tables())


def test_every_migration_table_is_owned_by_a_model_module():
    """迁移里的表集合 = 各模型模块的表并集:两边只改一处时立刻炸,不留下无主的表。"""
    owned = set(mig.model_tables("app.metering.tables")) | set(model_tables())
    assert owned == set(mig.migration_tables()), (
        f"迁移与模型模块的表集合不一致(模型 {sorted(owned)},迁移 {sorted(mig.migration_tables())})")
    assert set(METERING_TABLES) <= owned          # 计量那四张仍归 metering 模块所有


def test_email_column_is_matched_exactly_not_accent_insensitively():
    """users.email 必须显式 COLLATE utf8mb4_bin。

    库默认的 utf8mb4_0900_ai_ci 不区分大小写、也不区分重音:e 与 é 会被判成同一个地址,
    唯一键因此「意外合并」两个不同账号。规范化后邮箱整体小写(见 app/auth/emails.py),
    比较规则必须精确到字符相等,所以这里显式钉住排序规则。
    """
    assert _column("users", "email").collation == "utf8mb4_bin"
    # 其余文本列跟随库默认排序规则即可(不比较、不做唯一键),不能顺手全表改 bin。
    assert _column("users", "display_name").collation is None
    assert _column("auth_audit", "email").collation is None
