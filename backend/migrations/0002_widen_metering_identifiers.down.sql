-- 回滚 0002:把列宽改回 0001 的样子(按 up 的相反顺序)。
--
-- 只在空库 / 开发库上执行:只要表里已经有 36 位的 document_id 或超过 320 字符的 item_key
-- (索引过任何一份文档就一定有),严格模式下这条 ALTER 会直接报 1406 —— 线上不要执行。

ALTER TABLE call_event_items MODIFY COLUMN item_key    VARCHAR(320) NOT NULL;
ALTER TABLE cache_events     MODIFY COLUMN document_id CHAR(32)     NULL;
ALTER TABLE call_event_items MODIFY COLUMN document_id CHAR(32)     NULL;
ALTER TABLE call_events      MODIFY COLUMN document_id CHAR(32)     NULL;
