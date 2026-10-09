-- 时间、事件状态和消息级来源。旧行保持空对象，不从入库时间补造事件时间。
ALTER TABLE memory_items ADD COLUMN fact_context JSON NULL;
UPDATE memory_items SET fact_context = JSON_OBJECT() WHERE fact_context IS NULL;
ALTER TABLE memory_items MODIFY COLUMN fact_context JSON NOT NULL;
ALTER TABLE memory_history ADD COLUMN old_context JSON NULL;
ALTER TABLE memory_history ADD COLUMN new_context JSON NULL;
