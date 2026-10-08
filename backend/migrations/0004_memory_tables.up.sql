-- 0004 长期记忆：memory_items / memory_history / memory_jobs / memory_user_state 四张表。
-- 与 app/memory/tables.py 一一对应（tests/test_memory_migration.py 逐项比对列 / 键 / 索引）；
-- 改表结构请新增一版迁移，不要改这一版。执行方式见同目录 README。
--
-- 约定（与 docs/长期记忆系统技术方案.md 一致）：
--   * 时间一律 UTC、DATETIME(6)（微秒）；
--   * MySQL 是事实源，Qdrant 只是可重建索引：任何一条有效记忆都能从 memory_items
--     重建出向量，Qdrant 里不存任何「只在它那里有」的状态；
--   * 删除是软删（status='deleted' + deleted_at），行与 memory_history 都保留；
--     「用户数据彻底删除」是另一个动作（清正文 + 审计脱敏，见技术方案「删除语义」）；
--   * 记忆正文是用户私有数据，只在必要的列上停留：任务 payload 在任务进终态时清空；
--   * 不建外键（同 0001 / 0003）：删用户不得连带删调用历史、审计记录；
--   * 不写 server_default：NOT NULL 列的默认值全部在 Python 侧给，表结构与模型严格一致。

CREATE TABLE memory_items (
  id                CHAR(32)     NOT NULL COMMENT '记忆 id（uuid4().hex）',
  user_id           CHAR(36)     NOT NULL COMMENT '归属用户（users.id）；记忆按用户隔离、跨会话共享',
  text              TEXT         NOT NULL COMMENT '记忆正文（一句事实；写入时截断到配置上限）',
  content_hash      CHAR(32)     NOT NULL COMMENT '规范化正文的 MD5；同用户内去重用（软删行不参与）',
  status            VARCHAR(16)  NOT NULL COMMENT 'active / deleted',
  origin            VARCHAR(16)  NOT NULL COMMENT 'llm（会话提取）/ user（用户手添或编辑）',
  thread_id         VARCHAR(160) NULL     COMMENT '来源会话线程 id（手添为 NULL；列宽同 conversations.thread_id）',
  revision          INT          NOT NULL COMMENT '版本号：每次 UPDATE +1（从 1 起）',
  embedding_version VARCHAR(32)  NOT NULL COMMENT '最近写入向量索引时的 embeddings_version；空 = 未索引',
  indexed_at        DATETIME(6)  NULL     COMMENT '最近一次成功写入 Qdrant 的时间；NULL = 未索引',
  created_at        DATETIME(6)  NOT NULL,
  updated_at        DATETIME(6)  NOT NULL,
  deleted_at        DATETIME(6)  NULL     COMMENT '软删时间；NULL = 有效',
  PRIMARY KEY (id),
  KEY ix_memory_items_owner (user_id, status, updated_at),
  KEY ix_memory_items_hash (user_id, content_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 变更审计：只追加，不修改、不删除。ADD / UPDATE / DELETE 各留一行，
-- old_text / new_text 让「这条记忆怎么变成现在这样」可回溯；
-- 彻底删除用户数据时对正文列脱敏（不是删行 —— 审计线索要留下）。
CREATE TABLE memory_history (
  id         BIGINT       NOT NULL AUTO_INCREMENT,
  memory_id  CHAR(32)     NOT NULL,
  user_id    CHAR(36)     NOT NULL COMMENT '冗余归属：按用户查审计 / 彻底删除时不依赖 memory_items',
  event      VARCHAR(16)  NOT NULL COMMENT 'ADD / UPDATE / DELETE',
  old_text   TEXT         NULL     COMMENT '变更前正文（ADD 为 NULL）',
  new_text   TEXT         NULL     COMMENT '变更后正文（DELETE 为 NULL）',
  actor      VARCHAR(16)  NOT NULL COMMENT 'llm / user / system（谁改的）',
  reason     VARCHAR(255) NOT NULL COMMENT '变更原因摘要',
  thread_id  VARCHAR(160) NULL     COMMENT '触发本次变更的会话线程（非会话触发为 NULL）',
  created_at DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  KEY ix_memory_history_item (memory_id, id),
  KEY ix_memory_history_owner (user_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 后台任务（会话提取；预留后续维护任务）：持久化在 MySQL 里，进程重启不丢。
-- 认领 = 带条件的 UPDATE（compare-and-set）+ 租约；同一用户同一时刻只允许一个
-- running 任务（去重判断需要按用户串行），认领查询里显式避开有活跃租约的用户。
CREATE TABLE memory_jobs (
  id               CHAR(32)     NOT NULL COMMENT '任务 id（uuid4().hex）',
  user_id          CHAR(36)     NOT NULL,
  kind             VARCHAR(24)  NOT NULL COMMENT 'extract（会话提取）',
  status           VARCHAR(16)  NOT NULL COMMENT 'pending / running / succeeded / failed',
  dedupe_key       CHAR(64)     NOT NULL COMMENT '幂等键：同一轮对话重复入队只留一行',
  thread_id        VARCHAR(160) NULL,
  payload          JSON         NOT NULL COMMENT '任务输入（对话消息）；进终态时清成 {}，不在库里长留正文',
  generation       INT          NOT NULL COMMENT '入队时该用户的记忆代次；与 memory_user_state 不符即作废',
  attempts         INT          NOT NULL COMMENT '已执行次数（认领即 +1）',
  max_attempts     INT          NOT NULL COMMENT '上限：达到后不再重排，直接落 failed',
  next_run_at      DATETIME(6)  NOT NULL COMMENT '最早可执行时间（失败退避用）',
  lease_owner      VARCHAR(96)  NULL     COMMENT '租约持有者（host:pid:thread）',
  lease_expires_at DATETIME(6)  NULL     COMMENT '租约到期；过期视为持有者已死，可被重新认领',
  last_error       VARCHAR(500) NOT NULL COMMENT '最近一次失败的脱敏摘要',
  created_at       DATETIME(6)  NOT NULL,
  updated_at       DATETIME(6)  NOT NULL,
  finished_at      DATETIME(6)  NULL     COMMENT '进入终态（succeeded / failed）的时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_memory_jobs_dedupe (user_id, kind, dedupe_key),
  KEY ix_memory_jobs_claim (status, next_run_at),
  KEY ix_memory_jobs_owner (user_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 每用户一行的记忆代次（generation）：用户第一次写记忆或第一次「彻底删除」时建立。
-- 用途只有一个：让「清除记忆」之前的任务与决策失效 —— 入队时把当时的代次写进
-- memory_jobs.generation，执行与落库前比对，对不上就整批作废，被删掉的事实不会被
-- 旧任务悄悄写回来。行里不含任何正文，删除记忆也不删这一行（水位只增不减）。
CREATE TABLE memory_user_state (
  user_id    CHAR(36)    NOT NULL COMMENT 'users.id',
  generation INT         NOT NULL COMMENT '记忆代次：每次「彻底删除」+1；从 0 起',
  purged_at  DATETIME(6) NULL     COMMENT '最近一次彻底删除的时间（仅展示 / 排查用）',
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
