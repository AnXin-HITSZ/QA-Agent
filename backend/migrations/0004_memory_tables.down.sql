-- 0004 回滚：删掉长期记忆四张表。
-- 执行前先让新版代码下线，并确认记忆数据不再需要（连正文带审计一起删）。
-- Qdrant 里的用户记忆集合不随之清理 —— 那是本模块重建索引流程的职责（见技术方案「删除语义」）。
DROP TABLE IF EXISTS memory_user_state;
DROP TABLE IF EXISTS memory_jobs;
DROP TABLE IF EXISTS memory_history;
DROP TABLE IF EXISTS memory_items;
