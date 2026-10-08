-- 0005 的回滚：把 0005 加的东西原样撤掉（只回滚结构，不还原数据）。
--
-- 注意：DROP COLUMN 会**丢掉**这些列里的值（scope / 索引版本标记 / 阶段结果等）。
-- 回滚前请确认：这些列承载的信息在应用代码里已经不再被读取（即代码也回到 0004 那一版）。
-- 顺序与 0005 的 up 相反：先删后加的表，再从后往前删列。

DROP TABLE IF EXISTS memory_ops;
DROP TABLE IF EXISTS memory_sources;

ALTER TABLE memory_jobs
  DROP KEY ix_memory_jobs_scope,
  DROP COLUMN committed_at,
  DROP COLUMN outcome,
  DROP COLUMN stages,
  DROP COLUMN claim_token,
  DROP COLUMN turn_id,
  DROP COLUMN scope;

ALTER TABLE memory_items
  DROP KEY ix_memory_items_scope,
  DROP COLUMN indexed_meta_version,
  DROP COLUMN indexed_revision,
  DROP COLUMN meta_version,
  DROP COLUMN generation,
  DROP COLUMN event_time,
  DROP COLUMN kind,
  DROP COLUMN scope;
