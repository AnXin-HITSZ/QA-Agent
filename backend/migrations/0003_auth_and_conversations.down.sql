-- 0003 回滚：删掉认证、鉴权与用户管理六张表。
-- 顺序与建表相反；没有外键，顺序本身不强制，但保持可读。
-- 注意：执行前确认这些数据确实不再需要 —— 用户、会话、审计记录删掉就没了，
-- 且 Redis 里的对话 checkpoint 不会随之清理（那是 conversations 删除流程的职责）。

DROP TABLE IF EXISTS auth_audit;
DROP TABLE IF EXISTS conversations;
DROP TABLE IF EXISTS email_tokens;
DROP TABLE IF EXISTS auth_refresh_tokens;
DROP TABLE IF EXISTS auth_sessions;
DROP TABLE IF EXISTS users;
