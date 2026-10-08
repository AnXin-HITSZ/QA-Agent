-- 0005 长期记忆：作用域隔离 / 认领凭证（fencing）/ 阶段结果 / 版本化索引标记 / 来源与事件时间 /
--      可恢复的删除清理台账。与 app/memory/tables.py 一一对应（tests/test_memory_migration.py
--      逐项比对列 / 键 / 索引）。执行方式见同目录 README。
--
-- 为什么是**增量**：0004 已经在开发 / 生产执行过（见 README 的部署记录），改 0004 会让已经
-- 执行过它的库升级不上来。这里只加列 / 加表，不改任何既有列与既有键。
--
-- 写法定式（本目录的约定：NOT NULL 列的默认值在 Python 侧给，表结构里不写 DEFAULT）：
--   ADD COLUMN ... NULL  →  UPDATE 回填  →  MODIFY COLUMN ... NOT NULL
-- 三步走只为了给**存量行**一个值；应用侧不依赖 server_default，全部显式写入。
-- 0004 的库若还没有数据，这三步同样正确（UPDATE 影响 0 行）。

-- ---------------------------------------------------------------- memory_items

-- 作用域：'' = 正式（聊天产生的记忆）；'eval:<run-id>' = 离线评测。
-- 评测数据必须与正式数据在同一张表里也互不可见（普通 Worker 不认领评测任务、
-- 评测清理不删正式记录），scope 是这套隔离在 SQL 里的落点。
ALTER TABLE memory_items
  ADD COLUMN scope               VARCHAR(64) NULL COMMENT '作用域:空=正式;eval:<run-id>=评测(与正式数据严格隔离)',
  ADD COLUMN kind                VARCHAR(16) NULL COMMENT '事实类型:preference/profile/task;空=未标注(用户手添/老数据)',
  ADD COLUMN event_time          DATETIME(6) NULL COMMENT '事件发生时间(用户表述里的时间);未知为 NULL,不由系统推断',
  ADD COLUMN generation          INT          NULL COMMENT '写入时的记忆代次:清理按它过滤,只删本次清除之前的点',
  ADD COLUMN meta_version        INT          NULL COMMENT '元数据版本:仅非正文变更 +1(不触发重新向量化)',
  ADD COLUMN indexed_revision    INT          NULL COMMENT '已写入索引的正文版本(等于 revision 才算最新);0=从未索引',
  ADD COLUMN indexed_meta_version INT         NULL COMMENT '已写入索引的元数据版本',
  ADD KEY ix_memory_items_scope (scope, status, updated_at);

UPDATE memory_items
   SET scope = COALESCE(scope, ''),
       kind = COALESCE(kind, ''),
       generation = COALESCE(generation, 0),
       meta_version = COALESCE(meta_version, 0),
       indexed_revision = COALESCE(indexed_revision, 0),
       indexed_meta_version = COALESCE(indexed_meta_version, 0);

ALTER TABLE memory_items
  MODIFY COLUMN scope VARCHAR(64) NOT NULL COMMENT '作用域:空=正式;eval:<run-id>=评测(与正式数据严格隔离)';
ALTER TABLE memory_items
  MODIFY COLUMN kind VARCHAR(16) NOT NULL COMMENT '事实类型:preference/profile/task;空=未标注(用户手添/老数据)';
ALTER TABLE memory_items
  MODIFY COLUMN generation INT NOT NULL COMMENT '写入时的记忆代次:清理按它过滤,只删本次清除之前的点';
ALTER TABLE memory_items
  MODIFY COLUMN meta_version INT NOT NULL COMMENT '元数据版本:仅非正文变更 +1(不触发重新向量化)';
ALTER TABLE memory_items
  MODIFY COLUMN indexed_revision INT NOT NULL COMMENT '已写入索引的正文版本(等于 revision 才算最新);0=从未索引';
ALTER TABLE memory_items
  MODIFY COLUMN indexed_meta_version INT NOT NULL COMMENT '已写入索引的元数据版本';

-- ---------------------------------------------------------------- memory_jobs

-- 认领凭证（fencing token）：每次认领换一个随机串，收尾 / 续租 / 落库前都要一致。
-- 只用 lease_owner 是不够的：租约过期后同一个 worker 身份仍可能把「上一个实例的结论」
-- 写进去（owner 是 host:pid:随机，重启前的那份已经不可信）；claim_token 让「这一轮认领」
-- 与「上一次认领」在数据层可区分，旧持有者的提交会被条件更新直接拒绝。
ALTER TABLE memory_jobs
  ADD COLUMN scope        VARCHAR(64) NULL COMMENT '作用域:与 memory_items.scope 同一口径;认领/重放都按它过滤',
  ADD COLUMN turn_id      VARCHAR(160) NULL COMMENT '本任务对应的稳定轮次标识(来源关联的幂等键)',
  ADD COLUMN claim_token  CHAR(32)    NULL COMMENT '本次认领的凭证(fencing):收尾/续租/落库前必须一致',
  ADD COLUMN stages       JSON        NULL COMMENT '阶段结果:输入摘要/协议版本/模型口径/执行状态(终态清内容)',
  ADD COLUMN outcome      JSON        NULL COMMENT '执行结果摘要(计数与标记,不含正文;终态后保留供状态展示)',
  ADD COLUMN committed_at DATETIME(6) NULL COMMENT '事实提交时间;非空表示已提交,收尾重试不得重复执行',
  ADD KEY ix_memory_jobs_scope (scope, status, next_run_at);

UPDATE memory_jobs SET scope = COALESCE(scope, ''), claim_token = COALESCE(claim_token, '');

ALTER TABLE memory_jobs
  MODIFY COLUMN scope VARCHAR(64) NOT NULL COMMENT '作用域:与 memory_items.scope 同一口径;认领/重放都按它过滤';
ALTER TABLE memory_jobs
  MODIFY COLUMN claim_token CHAR(32) NOT NULL COMMENT '本次认领的凭证(fencing):收尾/续租/落库前必须一致';

-- ---------------------------------------------------------------- memory_sources

-- 记忆 ↔ 来源轮次：一条记忆可以来自多轮对话（同一件事在不同会话里被重复确认），
-- 不能只用 memory_items.thread_id 一个列表示（那会被最后一次写入覆盖）。
-- turn_id 是入队时登记的稳定轮次标识（优先于正文哈希，见 service.enqueue_extraction）：
-- 同样的正文在不同会话里是两个来源，不会被当成同一条。
CREATE TABLE memory_sources (
  id              BIGINT       NOT NULL AUTO_INCREMENT,
  memory_id       CHAR(32)     NOT NULL COMMENT '记忆 id（memory_items.id）',
  user_id         CHAR(36)     NOT NULL COMMENT '归属用户（users.id）：按用户彻底清除时不依赖其它表',
  scope           VARCHAR(64)  NOT NULL COMMENT '作用域：与 memory_items.scope 同一口径',
  conversation_id VARCHAR(160) NULL COMMENT '来源会话（与 memory_items.thread_id 同口径；用户手添为 NULL）',
  turn_id         VARCHAR(160) NOT NULL COMMENT '来源轮次的稳定标识（同一轮重复入队只记一次）',
  created_at      DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_memory_sources_turn (memory_id, turn_id),
  KEY ix_memory_sources_owner (user_id, conversation_id),
  KEY ix_memory_sources_memory (memory_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- memory_ops

-- 可恢复的清理台账：删除一条记忆（或彻底清除一个用户）时，**先在事实事务里登记**，
-- 再在事务外执行 Qdrant 删除；失败留下 pending 由 worker 按退避重试。
-- 没有这张表时删除的向量清理失败只会写一行日志 —— 没人会去看，也没人会重试。
-- generation 是 purge_user 的过滤依据：只删「小于当前代次」的点，清理期间用户新写的
-- 记忆（新代次）不会被这次清理波及。
CREATE TABLE memory_ops (
  id               CHAR(32)     NOT NULL COMMENT '操作 id（uuid4().hex）',
  user_id          CHAR(36)     NOT NULL,
  scope            VARCHAR(64)  NOT NULL COMMENT '作用域：与 memory_items.scope 同一口径',
  kind             VARCHAR(24)  NOT NULL COMMENT 'delete_vector（按 memory_id 清点）/ purge_user（按用户+代次清）',
  memory_id        CHAR(32)     NULL COMMENT 'kind=delete_vector 的目标记忆 id',
  generation       INT          NOT NULL COMMENT 'purge_user:只删该代次之前的点（不得波及清理之后新建的记忆）',
  payload          JSON         NULL COMMENT '执行参数（预留：批量删除时的 id 清单等）',
  status           VARCHAR(16)  NOT NULL COMMENT 'pending / running / succeeded / failed',
  attempts         INT          NOT NULL,
  max_attempts     INT          NOT NULL,
  next_run_at      DATETIME(6)  NOT NULL,
  lease_owner      VARCHAR(96)  NULL,
  claim_token      CHAR(32)     NOT NULL COMMENT '本次认领的凭证（fencing）：收尾条件更新必须一致',
  lease_expires_at DATETIME(6)  NULL,
  last_error       VARCHAR(500) NOT NULL,
  created_at       DATETIME(6)  NOT NULL,
  updated_at       DATETIME(6)  NOT NULL,
  finished_at      DATETIME(6)  NULL,
  PRIMARY KEY (id),
  KEY ix_memory_ops_claim (scope, status, next_run_at),
  KEY ix_memory_ops_owner (user_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
