-- 回滚会丢失时间精度、事件状态及消息级来源；先备份。
ALTER TABLE memory_items DROP COLUMN fact_context;
ALTER TABLE memory_history DROP COLUMN new_context;
ALTER TABLE memory_history DROP COLUMN old_context;
