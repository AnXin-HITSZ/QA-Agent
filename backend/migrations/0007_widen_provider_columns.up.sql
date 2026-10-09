-- 0007 放宽供应商列:call_events.provider / price_config.provider VARCHAR(32) → VARCHAR(255)。
--
-- 背景(生产实测):provider 的取值是 provider_of(base_url) —— 端点**主机名**,不是短品牌名。
-- 2026-10-08 rerank 接到新网关 llm-hh23ndoe58qsq2rn.cn-beijing.maas.aliyuncs.com(49 字符),
-- 超过 VARCHAR(32):MySQL 严格模式下整批 INSERT 报 1406 Data too long,凡是与 rerank 同批的
-- 记录(包括正常的 embedding / llm)一条都进不了库,60 条全部落入补写目录、反复重试进不去
-- —— 与 0002 是同一类坑(见 0002 的背景说明),这次载在 provider 上。
--
-- 宽度对齐同表的 endpoint(VARCHAR(255)):provider 与 endpoint 是同一个主机名,DNS 主机名
-- 上限 253 字符,255 就是这类取值的天花板,以后换任何网关都不会再撞。
-- price_config.provider 与 call_events.provider 同一取值来源,必须一起放宽,否则长主机名
-- 的价目永远配置不进去(同样撞 1406)。唯一键 uq_price_config_rule 含 provider:
-- utf8mb4 下最坏 255×4 + 其余列 ≈ 1.5KB,没超 InnoDB 的 3072 字节索引上限。
--
-- 与 app/metering/tables.py 一一对应(改表结构:先改模型,再新增一版迁移);
-- 执行方式见同目录 README。

ALTER TABLE call_events  MODIFY COLUMN provider VARCHAR(255) NOT NULL;
ALTER TABLE price_config MODIFY COLUMN provider VARCHAR(255) NOT NULL;
