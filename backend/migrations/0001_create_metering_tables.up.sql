-- 0001 建调用日志与费用统计的四张表：call_events / call_event_items / cache_events / price_config。
-- 与 app/metering/tables.py 一一对应（tests/test_metering_migration.py 逐项比对列 / 键 / 索引）；
-- 改表结构请新增一版迁移，不要改这一版。执行方式见同目录 README。
--
-- 约定（与 docs/调用日志与费用统计技术方案.md §4 一致）：
--   * 时间一律 UTC、DATETIME(6)（微秒）；金额 DECIMAL(18,8)、用量 DECIMAL(20,6)；
--   * 金额 / 用量拿不到就留 NULL，不写 0（超时 ≠ 免费）；
--   * 主键 / 唯一键即幂等约束：补写重放靠它们天然去重，不会重复计数；
--   * 不建外键：删文件 / 删索引任务 / 清缓存都不得级联删调用历史；
--   * 不写 server_default：NOT NULL 列的默认值全部在 Python 侧给，表结构与模型严格一致。

CREATE TABLE call_events (
  event_id            CHAR(32)      NOT NULL COMMENT '事件 id(补写重放幂等键)',
  occurred_at         DATETIME(6)   NOT NULL COMMENT '请求开始时间(UTC)',
  created_at          DATETIME(6)   NOT NULL COMMENT '入库时间(UTC)',
  service             VARCHAR(16)   NOT NULL COMMENT 'embedding / ocr',
  purpose             VARCHAR(24)   NOT NULL COMMENT 'document_index / query',
  provider            VARCHAR(32)   NOT NULL,
  target              VARCHAR(64)   NOT NULL COMMENT '模型名 / OCR Type',
  endpoint            VARCHAR(255)  NOT NULL COMMENT '脱敏后的端点(仅主机名)',
  call_group          CHAR(32)      NULL     COMMENT '同一逻辑调用的多次尝试共享',
  attempt_no          SMALLINT      NOT NULL,
  retry_of            CHAR(32)      NULL     COMMENT '上一次尝试的事件 id',
  http_attempts       SMALLINT      NOT NULL COMMENT '本行背后的真实 HTTP 请求次数',
  duration_ms         INT           NOT NULL,
  status              VARCHAR(16)   NOT NULL COMMENT 'success / failure',
  error_class         VARCHAR(64)   NOT NULL,
  error_message       VARCHAR(300)  NOT NULL COMMENT '脱敏后的摘要',
  http_status         SMALLINT      NULL,
  provider_request_id VARCHAR(128)  NULL,
  usage_quantity      DECIMAL(20,6) NULL     COMMENT '用量;NULL = 未取得',
  usage_unit          VARCHAR(16)   NOT NULL,
  usage_source        VARCHAR(16)   NOT NULL,
  usage_note          VARCHAR(255)  NOT NULL,
  billing_quantity    DECIMAL(20,6) NULL,
  billing_unit        VARCHAR(16)   NOT NULL,
  billing_status      VARCHAR(16)   NOT NULL,
  billing_note        VARCHAR(255)  NOT NULL,
  cost_amount         DECIMAL(18,8) NULL     COMMENT '估算费用;NULL = 无法估算',
  currency            CHAR(3)       NOT NULL,
  cost_status         VARCHAR(16)   NOT NULL,
  cost_note           VARCHAR(255)  NOT NULL,
  price_id            BIGINT        NULL,
  price_version       VARCHAR(64)   NOT NULL,
  price_snapshot      JSON          NULL     COMMENT '事件发生时的价目快照(不可变)',
  job_id              VARCHAR(64)   NULL,
  document_id         CHAR(32)      NULL,
  oss_key             VARCHAR(512)  NULL,
  page_no             INT           NULL,
  PRIMARY KEY (event_id),
  KEY ix_call_events_time_service (occurred_at, service),
  KEY ix_call_events_job (job_id),
  KEY ix_call_events_document (document_id),
  KEY ix_call_events_group (call_group)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE call_event_items (
  id              BIGINT        NOT NULL AUTO_INCREMENT,
  event_id        CHAR(32)      NOT NULL,
  item_key        VARCHAR(320)  NOT NULL COMMENT 'NULL 字段的占位(norm)',
  document_id     CHAR(32)      NULL,
  oss_key         VARCHAR(512)  NULL,
  page_no         INT           NULL,
  text_count      INT           NOT NULL,
  allocated_cost  DECIMAL(18,8) NULL     COMMENT '分摊金额(估算)',
  allocation_note VARCHAR(64)   NOT NULL,
  created_at      DATETIME(6)   NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_call_event_items_event_item (event_id, item_key),
  KEY ix_call_event_items_document (document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE cache_events (
  id            BIGINT       NOT NULL AUTO_INCREMENT,
  event_id      CHAR(32)     NOT NULL,
  occurred_at   DATETIME(6)  NOT NULL,
  created_at    DATETIME(6)  NOT NULL,
  layer         VARCHAR(16)  NOT NULL COMMENT 'ocr_raw / ocr_text / embedding',
  purpose       VARCHAR(24)  NOT NULL,
  unit          VARCHAR(16)  NOT NULL COMMENT 'page / text(不同单位不合并)',
  hit_count     INT          NOT NULL,
  miss_count    INT          NOT NULL,
  shared_count  INT          NOT NULL,
  skipped_count INT          NOT NULL,
  job_id        VARCHAR(64)  NULL,
  document_id   CHAR(32)     NULL,
  oss_key       VARCHAR(512) NULL,
  page_no       INT          NULL,
  note          VARCHAR(255) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_cache_events_event_id (event_id),
  KEY ix_cache_events_time_layer (occurred_at, layer),
  KEY ix_cache_events_job (job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE price_config (
  id             BIGINT        NOT NULL AUTO_INCREMENT,
  service        VARCHAR(16)   NOT NULL,
  provider       VARCHAR(32)   NOT NULL,
  target         VARCHAR(64)   NOT NULL COMMENT '模型 / OCR Type;空 = 通用',
  unit           VARCHAR(16)   NOT NULL COMMENT '1k_tokens / request / page',
  currency       CHAR(3)       NOT NULL,
  unit_price     DECIMAL(18,8) NOT NULL,
  effective_from DATETIME(6)   NOT NULL,
  source         VARCHAR(255)  NOT NULL COMMENT '官方价格来源与核实日期',
  note           VARCHAR(255)  NOT NULL,
  created_at     DATETIME(6)   NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_price_config_rule (service, provider, target, unit, currency, effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
