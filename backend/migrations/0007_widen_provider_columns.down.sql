-- 回滚 0007:把 provider 列宽改回 VARCHAR(32)(按 up 的相反顺序)。
--
-- 只在确认表里没有超过 32 字符的 provider 时执行:严格模式下,只要有一行放不下,
-- 这条 ALTER 直接报 1406 —— 触发 0007 的那批记录就是 49 字符的 rerank 网关主机名,
-- 生产库应用 0007 之后(历史数据还在)行里必然有超宽值,线上不要执行。

ALTER TABLE price_config MODIFY COLUMN provider VARCHAR(32) NOT NULL;
ALTER TABLE call_events  MODIFY COLUMN provider VARCHAR(32) NOT NULL;
