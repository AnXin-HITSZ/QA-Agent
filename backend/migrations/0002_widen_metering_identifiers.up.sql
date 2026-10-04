-- 0002 放宽标识列:document_id CHAR(32) → CHAR(36)、item_key VARCHAR(320) → VARCHAR(549)。
--
-- 背景(生产实测):document_id 是 app/rag/documents.py 登记的文档身份,取的是
-- str(uuid.uuid4()) —— 带连字符共 36 位,CHAR(32) 装不下。MySQL 严格模式下整批 INSERT
-- 报 1406 Data too long,带 document_id 的索引类事件一条都进不了库,全积压在补写目录里
-- 反复重试。本地测试跑 SQLite,而 SQLite 不校验 CHAR / VARCHAR 长度,这个错只有 MySQL 会暴露。
--
-- item_key = document_id + ":" + oss_key,最长 36 + 1 + 512 = 549 字符:原来的 VARCHAR(320)
-- 会在长路径上撞同一个 1406(oss_key 列本身允许 512)。唯一键 (event_id, item_key) 在
-- utf8mb4 下最坏 32×4 + 549×4 = 2324 字节,没超 InnoDB 的 3072 字节索引上限。
--
-- 与 app/metering/tables.py 一一对应(改表结构:先改模型,再新增一版迁移);
-- 执行方式见同目录 README。

ALTER TABLE call_events      MODIFY COLUMN document_id CHAR(36)     NULL;
ALTER TABLE call_event_items MODIFY COLUMN document_id CHAR(36)     NULL;
ALTER TABLE cache_events     MODIFY COLUMN document_id CHAR(36)     NULL;
ALTER TABLE call_event_items MODIFY COLUMN item_key    VARCHAR(549) NOT NULL;
