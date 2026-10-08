-- 回滚前停止 Worker；评测作用域的独立水位会丢失，评测应重建。
-- 执行本脚本前，运维须先清理评测数据（包括跨作用域重复 jobs）；本脚本不代替该前置步骤。
ALTER TABLE memory_jobs DROP KEY uk_memory_jobs_dedupe,
  ADD UNIQUE KEY uk_memory_jobs_dedupe (user_id, kind, dedupe_key);
DELETE FROM memory_user_state WHERE scope <> '';
ALTER TABLE memory_user_state DROP PRIMARY KEY, ADD PRIMARY KEY (user_id);
ALTER TABLE memory_user_state DROP COLUMN scope;
ALTER TABLE memory_history DROP COLUMN scope;
