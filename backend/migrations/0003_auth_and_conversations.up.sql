-- 0003 认证、鉴权与用户管理：users / auth_sessions / auth_refresh_tokens / email_tokens /
--      conversations / auth_audit 六张表。
-- 与 app/auth/tables.py 一一对应（tests/test_auth_migration.py 逐项比对列 / 键 / 索引）；
-- 改表结构请新增一版迁移，不要改这一版。执行方式见同目录 README。
--
-- 约定（与 docs/认证鉴权与用户管理技术方案.md §7 一致）：
--   * 时间一律 UTC、DATETIME(6)（微秒）；
--   * 令牌（refresh / 邮箱验证 / 重置密码）只存 SHA-256 摘要，库里不出现任何明文令牌；
--     密码只存 Argon2id 编码串（含参数与盐，自带盐，故无独立盐列）；
--   * 会话 / 令牌行是安全状态与审计线索：过期只清行，不级联删别的表；
--   * 不建外键（同 0001）：删用户不得连带删调用历史、审计记录；
--   * 不写 server_default：NOT NULL 列的默认值全部在 Python 侧给，表结构与模型严格一致。
--
-- 邮箱列显式 COLLATE utf8mb4_bin：规范化后整体小写（见 app/auth/emails.py），唯一键必须按
-- 「精确字符串相等」判定，不能跟随库默认的 utf8mb4_0900_ai_ci —— 那个排序规则不区分大小写、
-- **也不区分重音**，会把 é 与 e 判成同一地址而意外合并两个账号。
-- 用户名部分不做 Gmail 式点号合并、不删 +标签（不按某家提供商的规则合并不同地址）。

CREATE TABLE users (
  id                CHAR(36)     NOT NULL COMMENT '用户 id（UUID）',
  email             VARCHAR(254) COLLATE utf8mb4_bin NOT NULL COMMENT '规范化邮箱：去首尾空白、整体小写；唯一键即精确匹配',
  password_hash     VARCHAR(255) NOT NULL COMMENT 'Argon2id 编码串（$argon2id$...，含参数与盐）',
  display_name      VARCHAR(64)  NOT NULL COMMENT '展示名',
  role              VARCHAR(16)  NOT NULL COMMENT 'user / admin（注册一律 user，首个 admin 由命令行建立）',
  status            VARCHAR(24)  NOT NULL COMMENT 'pending_email / pending_approval / active / rejected / disabled',
  auth_version      INT          NOT NULL COMMENT '令牌版本：改密 / 重置 / 强制下线时 +1，旧 access token 立即失效',
  email_verified_at DATETIME(6)  NULL     COMMENT '邮箱验证通过时间；NULL = 未验证',
  reviewed_at       DATETIME(6)  NULL     COMMENT '管理员审批（通过 / 拒绝）时间',
  reviewed_by       CHAR(36)     NULL     COMMENT '审批管理员 id',
  review_note       VARCHAR(255) NOT NULL COMMENT '审批备注（拒绝原因等；无则空串）',
  created_at        DATETIME(6)  NOT NULL,
  updated_at        DATETIME(6)  NOT NULL,
  last_login_at     DATETIME(6)  NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_users_email (email),
  KEY ix_users_status (status, created_at),
  KEY ix_users_role (role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 登录会话：一行 = 一次登录（一个设备），也是 refresh token 家族的锚点。
-- 撤销按行标记（revoked_at），不删行 —— 撤销原因要留痕（登出 / 改密 / 重放检测 / 管理员禁用）。
CREATE TABLE auth_sessions (
  id             CHAR(36)     NOT NULL COMMENT '会话 id（sid，写进 access token）',
  user_id        CHAR(36)     NOT NULL,
  user_agent     VARCHAR(255) NOT NULL COMMENT '登录时 UA 摘要（截断；会话列表展示用）',
  ip             VARCHAR(45)  NOT NULL COMMENT '登录来源 IP（IPv6 最长 45）',
  created_at     DATETIME(6)  NOT NULL,
  last_used_at   DATETIME(6)  NOT NULL COMMENT '最近一次刷新时间（列表排序用；不延长有效期）',
  expires_at     DATETIME(6)  NOT NULL COMMENT '绝对过期时间，不随刷新顺延',
  revoked_at     DATETIME(6)  NULL     COMMENT 'NULL = 有效',
  revoked_reason VARCHAR(32)  NOT NULL COMMENT 'logout / logout_all / password_change / password_reset / rotated_replay / admin_disable / admin_delete',
  PRIMARY KEY (id),
  KEY ix_auth_sessions_user (user_id, revoked_at),
  KEY ix_auth_sessions_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- refresh token 轮换链：每次刷新新增一行、把旧行标记 consumed_at（不覆盖），
-- 于是「已被消费的旧令牌又被拿来用」可判定为重放，撤销整个会话（宽限窗口见 app/auth/service.py）。
CREATE TABLE auth_refresh_tokens (
  id          CHAR(36)    NOT NULL,
  session_id  CHAR(36)    NOT NULL,
  user_id     CHAR(36)    NOT NULL,
  token_hash  CHAR(64)    NOT NULL COMMENT 'refresh token 的 SHA-256 十六进制摘要（原文不落库）',
  created_at  DATETIME(6) NOT NULL,
  consumed_at DATETIME(6) NULL     COMMENT 'NULL = 当前有效的那一枚；非 NULL = 已轮换掉',
  PRIMARY KEY (id),
  UNIQUE KEY uk_auth_refresh_tokens_hash (token_hash),
  KEY ix_auth_refresh_tokens_session (session_id, consumed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 邮箱令牌：验证邮箱 / 重置密码共用一张表，purpose 区分用途（两类互不通用）。
-- 一次性：消费即写 consumed_at（原子 UPDATE ... WHERE consumed_at IS NULL）。
CREATE TABLE email_tokens (
  id          CHAR(36)    NOT NULL,
  user_id     CHAR(36)    NOT NULL,
  purpose     VARCHAR(20) NOT NULL COMMENT 'verify_email / reset_password',
  token_hash  CHAR(64)    NOT NULL COMMENT '令牌的 SHA-256 十六进制摘要（原文只出现在邮件链接里）',
  created_at  DATETIME(6) NOT NULL,
  expires_at  DATETIME(6) NOT NULL,
  consumed_at DATETIME(6) NULL     COMMENT 'NULL = 未使用',
  ip          VARCHAR(45) NOT NULL COMMENT '申请来源 IP',
  PRIMARY KEY (id),
  UNIQUE KEY uk_email_tokens_hash (token_hash),
  KEY ix_email_tokens_user (user_id, purpose, consumed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 会话目录：前端「历史对话」列的是这张表（按 user_id 分页），不再扫描 Redis 里的全部线程。
-- checkpointer 里的内部线程 id 见 app/auth/conversations.py：qa:{env}:chat:v1:u:{user_id}:c:{id}。
-- 删除是两阶段：deleting（先落库、再取消 / 等待在途生成）→ 清 Redis checkpoint → deleted（墓碑，
-- 防止在途请求按旧 id 复活会话）。tombstone 行保留，列表只列 active。
CREATE TABLE conversations (
  id         CHAR(36)     NOT NULL COMMENT '会话 id',
  user_id    CHAR(36)     NOT NULL COMMENT '归属用户（归属校验的唯一依据，admin 看到的也是自己的）',
  thread_id  VARCHAR(160) NOT NULL COMMENT 'Redis checkpointer 线程 id（含环境标识，避免多环境串数据）',
  title      VARCHAR(100) NOT NULL COMMENT '标题（首条用户消息截断；空则占位）',
  status     VARCHAR(16)  NOT NULL COMMENT 'active / deleting / deleted',
  created_at DATETIME(6)  NOT NULL,
  updated_at DATETIME(6)  NOT NULL COMMENT '最近一条消息时间（列表排序键）',
  deleted_at DATETIME(6)  NULL     COMMENT 'status = deleted 的时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_conversations_thread (thread_id),
  KEY ix_conversations_owner (user_id, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 认证 / 用户管理审计：注册、验证、审批、禁用、改角色、登录失败、重放检测、首个管理员建立……
-- 只追加，不修改、不删除。actor / target 都可能是 NULL（如登录失败时目标尚不确定）。
CREATE TABLE auth_audit (
  id             BIGINT       NOT NULL AUTO_INCREMENT,
  occurred_at    DATETIME(6)  NOT NULL,
  action         VARCHAR(32)  NOT NULL COMMENT 'register / verify_email / approve / reject / disable / enable / role_change / login / login_failed / logout / logout_all / refresh_replay / password_change / password_reset / admin_bootstrap / cleanup',
  result         VARCHAR(16)  NOT NULL COMMENT 'ok / denied / failed',
  actor_user_id  CHAR(36)     NULL     COMMENT '操作者（本人行为 = 本人；审批类 = 管理员）',
  target_user_id CHAR(36)     NULL,
  email          VARCHAR(254) NOT NULL COMMENT '涉事邮箱（规范化后；失败事件也可能只有邮箱）',
  ip             VARCHAR(45)  NOT NULL,
  note           VARCHAR(255) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_auth_audit_time (occurred_at),
  KEY ix_auth_audit_action (action, occurred_at),
  KEY ix_auth_audit_target (target_user_id, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
