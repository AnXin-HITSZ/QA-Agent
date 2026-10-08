-- 清除代次和审计同样按 (user_id, scope) 隔离。先停止旧版 Worker，再执行迁移。
ALTER TABLE memory_history ADD COLUMN scope VARCHAR(64) NULL COMMENT '审计作用域:空=正式;eval:<run-id>=评测';
UPDATE memory_history h LEFT JOIN memory_items i ON i.id = h.memory_id
   SET h.scope = COALESCE(i.scope, '');
ALTER TABLE memory_history MODIFY COLUMN scope VARCHAR(64) NOT NULL COMMENT '审计作用域:空=正式;eval:<run-id>=评测';
ALTER TABLE memory_user_state ADD COLUMN scope VARCHAR(64) NULL COMMENT '记忆代次作用域';
UPDATE memory_user_state SET scope = '';
ALTER TABLE memory_user_state MODIFY COLUMN scope VARCHAR(64) NOT NULL COMMENT '记忆代次作用域';
ALTER TABLE memory_user_state DROP PRIMARY KEY, ADD PRIMARY KEY (user_id, scope);
ALTER TABLE memory_jobs DROP KEY uk_memory_jobs_dedupe,
  ADD UNIQUE KEY uk_memory_jobs_dedupe (user_id, kind, scope, dedupe_key);
-- 评测存量数据沿用迁移前的用户水位，避免有效任务被误判为过时代次。
INSERT INTO memory_user_state (user_id, scope, generation, purged_at, created_at, updated_at)
SELECT DISTINCT i.user_id, i.scope, s.generation, s.purged_at, s.created_at, s.updated_at
  FROM memory_items i JOIN memory_user_state s ON s.user_id = i.user_id AND s.scope = ''
 WHERE i.scope <> '';
INSERT INTO memory_user_state (user_id, scope, generation, purged_at, created_at, updated_at)
SELECT DISTINCT j.user_id, j.scope, s.generation, s.purged_at, s.created_at, s.updated_at
  FROM memory_jobs j JOIN memory_user_state s ON s.user_id = j.user_id AND s.scope = ''
 WHERE j.scope <> ''
ON DUPLICATE KEY UPDATE generation = VALUES(generation);
