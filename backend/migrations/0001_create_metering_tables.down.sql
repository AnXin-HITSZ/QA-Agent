-- 0001 回滚：删除调用日志与费用统计的四张表。
-- 表里的调用历史与价格配置一并丢失，只用于尚未上线的环境；
-- 线上执行前先确认这些记录不再需要（方案 §13：调用历史即审计线索，删除不可逆）。

DROP TABLE IF EXISTS price_config;
DROP TABLE IF EXISTS cache_events;
DROP TABLE IF EXISTS call_event_items;
DROP TABLE IF EXISTS call_events;
