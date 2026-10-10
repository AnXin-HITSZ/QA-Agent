-- 0009 记忆图:实体 / 别名 / 提及 + 消歧证据 / 关系 / 事件 + 参与者 / 事实来源绑定。
-- 与 app/memory/graph/tables.py 一一对应（tests/test_memory_graph_migration.py 逐项比对
-- 列 / 类型 / 可空 / 主键 / 唯一键 / 索引）。执行方式见同目录 README。
--
-- 定位（记忆图技术方案 §数据模型）:MySQL 是唯一事实源;Neo4j 只是这些行的**可重建投影**。
-- 本迁移只建 MySQL 表,不涉及 Neo4j;应用侧不建表、不改表（迁移由人手工执行）。
--
-- 写法定式（本目录约定:NOT NULL 列的默认值在 Python 侧给,表结构里不写 DEFAULT;
-- 不建外键;时间一律 DATETIME(6)、UTC 语义由代码保证)。
--
-- 关键设计:
--   * memory_entities 的 (user_id, scope, normalized) **没有**唯一键 —— 同名 ≠ 同一实体,
--     重名合并是消歧决策（证据在 memory_entity_mentions.evidence），不是唯一键能代替的;
--   * 关系 / 事件的多来源在 memory_fact_links（绑定原始记忆的**有效版本** revision,
--     移除一个来源不得删除仍有支持的事实）;
--   * 提及 / 参与者未消歧时 entity_id 为 NULL —— 保留待定,绝不强行绑定。
--
-- 回滚见 0009_memory_graph_tables.down.sql（只回滚结构,不还原数据）。

-- ---------------------------------------------------------------- memory_entities

-- 实体:稳定 ID + 规范名 + 类型（可扩展,'unknown' 可表示）。同名 ≠ 同一实体,故无名字唯一键。
CREATE TABLE memory_entities (
  id          CHAR(32)     NOT NULL COMMENT '实体 id（uuid4().hex;跨表引用稳定,重名不合并）',
  user_id     CHAR(36)     NOT NULL COMMENT '归属用户（users.id）:消歧与检索都限定在用户内',
  scope       VARCHAR(64)  NOT NULL COMMENT '作用域:与 memory_items.scope 同一口径',
  kind        VARCHAR(24)  NOT NULL COMMENT '实体类型:person/place/org/product/animal/topic/unknown…（可扩展）',
  name        TEXT         NOT NULL COMMENT '规范名（原文语言）;显示与回答引用用这一份',
  normalized  CHAR(32)     NOT NULL COMMENT '规范化摘要（NFKC+casefold）:精确匹配的检索键,不是唯一键（同名≠同一实体）',
  status      VARCHAR(16)  NOT NULL COMMENT 'active / merged（并入他实体）/ deleted（软删,保留审计）',
  generation  INT          NOT NULL COMMENT '写入时的记忆代次:彻底清除后旧任务不得回写',
  revision    INT          NOT NULL COMMENT '图元素版本:维护更新 +1（对账 / 投影核验用）',
  created_at  DATETIME(6)  NOT NULL,
  updated_at  DATETIME(6)  NOT NULL,
  deleted_at  DATETIME(6)  NULL     COMMENT '软删时间;非 NULL 时 status=deleted',
  PRIMARY KEY (id),
  KEY ix_memory_entities_owner (user_id, scope, status, updated_at),
  KEY ix_memory_entities_name (user_id, scope, normalized)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- memory_entity_aliases

-- 确认别名:同一实体的另一种说法。唯一键 (entity_id, normalized) 只约束「同一实体内不重复」,
-- 不同实体可以共享同一个别名（消歧失败时宁可各留各的,也不合并）。
CREATE TABLE memory_entity_aliases (
  id         BIGINT       NOT NULL AUTO_INCREMENT,
  entity_id  CHAR(32)     NOT NULL COMMENT 'memory_entities.id',
  user_id    CHAR(36)     NOT NULL,
  scope      VARCHAR(64)  NOT NULL COMMENT '作用域:与 memory_items.scope 同一口径',
  alias      TEXT         NOT NULL COMMENT '别名原文（如全名 / 昵称 / 缩写）',
  normalized CHAR(32)     NOT NULL COMMENT '规范化摘要（与实体规范名同一算法）',
  origin     VARCHAR(16)  NOT NULL COMMENT '来源:llm（提取确认）/ user（用户手动确认）',
  created_at DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_memory_entity_aliases (entity_id, normalized),
  KEY ix_memory_entity_aliases_lookup (user_id, scope, normalized)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- memory_entity_mentions

-- 提及 + 消歧证据:entity_id 为 NULL = 待定 / 未消歧（**绝不强行绑定**）。
-- 证据（匹配到哪个别名 / 类型 / 上下文摘要）随行保存;同名候选模糊时留 pending 交维护裁决。
CREATE TABLE memory_entity_mentions (
  id         BIGINT       NOT NULL AUTO_INCREMENT,
  entity_id  CHAR(32)     NULL     COMMENT '消歧结果;NULL = 待定 / 未消歧（绝不强行绑定）',
  user_id    CHAR(36)     NOT NULL,
  scope      VARCHAR(64)  NOT NULL COMMENT '作用域:与 memory_items.scope 同一口径',
  memory_id  CHAR(32)     NULL     COMMENT '提及出现的记忆（memory_items.id;可为空）',
  turn_key   VARCHAR(160) NOT NULL COMMENT '来源轮次;空串 = 无轮次来源（用户手添 / 旧数据）',
  surface    TEXT         NOT NULL COMMENT '提及的原文形态（保留原样,不改写）',
  normalized CHAR(32)     NOT NULL COMMENT '提及的规范化摘要（幂等去重用）',
  speaker    VARCHAR(160) NULL     COMMENT '说话人（适配层提供;无则 NULL）',
  status     VARCHAR(16)  NOT NULL COMMENT 'resolved / pending（候选模糊,待裁决）/ unresolved（暂不绑定）',
  evidence   JSON         NOT NULL COMMENT '消歧证据:匹配到的别名 / 类型 / 上下文摘要（不存大段正文）',
  generation INT          NOT NULL COMMENT '写入时的记忆代次',
  created_at DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_memory_entity_mentions (memory_id, turn_key, normalized),
  KEY ix_memory_entity_mentions_owner (user_id, scope, status),
  KEY ix_memory_entity_mentions_entity (entity_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- memory_relations

-- 关系:主语实体 → 类型 → 宾语（实体或原文短语）。方向有意义;多来源在 memory_fact_links。
CREATE TABLE memory_relations (
  id                CHAR(32)     NOT NULL COMMENT '关系 id（uuid4().hex;同一关系(主语/类型/宾语)重复出现时复用本行加来源）',
  user_id           CHAR(36)     NOT NULL,
  scope             VARCHAR(64)  NOT NULL COMMENT '作用域:与 memory_items.scope 同一口径',
  subject_entity_id CHAR(32)     NOT NULL COMMENT '主语实体（memory_entities.id）;方向不得反转',
  relation_type     VARCHAR(64)  NOT NULL COMMENT '通用可验证的关系类型（related_to/works_at/gift_from…;提取协议校验后入库）',
  object_entity_id  CHAR(32)     NULL     COMMENT '宾语实体;宾语不是实体时 NULL（见 object_text）',
  object_text       TEXT         NOT NULL COMMENT '宾语非实体时的原文短语;宾语是实体时空串',
  object_normalized CHAR(32)     NOT NULL COMMENT '宾语去重键:宾语是实体时为实体 id,否则为原文摘要（TEXT 不能索引,去重查询靠这一列）',
  memory_id         CHAR(32)     NOT NULL COMMENT '首个来源记忆;完整来源见 memory_fact_links',
  event_time        DATETIME(6)  NULL     COMMENT '关系发生 / 成立时间;未知为 NULL（不虚构）',
  state             VARCHAR(16)  NOT NULL COMMENT 'unknown/planned/ongoing/completed/cancelled（与事实 state 同口径）',
  status            VARCHAR(16)  NOT NULL COMMENT 'active / deleted（来源全部撤回或维护删除后软删）',
  generation        INT          NOT NULL COMMENT '写入时的记忆代次',
  revision          INT          NOT NULL COMMENT '图元素版本:维护更新 +1',
  created_at        DATETIME(6)  NOT NULL,
  updated_at        DATETIME(6)  NOT NULL,
  deleted_at        DATETIME(6)  NULL     COMMENT '软删时间;非 NULL 时 status=deleted',
  PRIMARY KEY (id),
  KEY ix_memory_relations_subject (user_id, scope, subject_entity_id, status),
  KEY ix_memory_relations_object (user_id, scope, object_entity_id, status),
  KEY ix_memory_relations_memory (memory_id),
  KEY ix_memory_relations_dedupe (user_id, scope, subject_entity_id, relation_type, object_normalized)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- memory_events

-- 事件:不是三元组。原始时间表述 + 锚点 + 可确定范围（精度如实）+ 状态 + 有依据的属性。
CREATE TABLE memory_events (
  id              CHAR(32)     NOT NULL COMMENT '事件 id（uuid4().hex;独立事件才有独立 id —— 计数题的去重依据）',
  user_id         CHAR(36)     NOT NULL,
  scope           VARCHAR(64)  NOT NULL COMMENT '作用域:与 memory_items.scope 同一口径',
  kind            VARCHAR(24)  NOT NULL COMMENT '事件类型（trip/meeting/purchase/…可扩展;unknown 可表示）',
  description     TEXT         NOT NULL COMMENT '事件描述（原文语言,保留参与者与对象）',
  normalized      CHAR(32)     NOT NULL COMMENT '规范化描述摘要:同一次事件的重复提及据此合并（不是两条事件）',
  time_expression VARCHAR(200) NOT NULL COMMENT '原始时间表述（原样摘录;空串 = 无时间表述）',
  anchor          DATETIME(6)  NULL     COMMENT '相对时间的锚点（来源记录时间）;无锚点不解析',
  start_at        DATETIME(6)  NULL     COMMENT '可确定的范围起点（仅按有依据的精度:仅年就圈整年）',
  end_at          DATETIME(6)  NULL     COMMENT '可确定的范围终点',
  time_precision  VARCHAR(16)  NOT NULL COMMENT 'year/month/day/minute/unknown（不虚构精度）',
  status          VARCHAR(16)  NOT NULL COMMENT 'planned/ongoing/completed/cancelled/unknown（计划与已发生必须区分）',
  attributes      JSON         NOT NULL COMMENT '有依据的属性键值（金额 / 地点等原文明确项;不虚构）',
  memory_id       CHAR(32)     NOT NULL COMMENT '首个来源记忆;完整来源见 memory_fact_links',
  generation      INT          NOT NULL COMMENT '写入时的记忆代次',
  revision        INT          NOT NULL COMMENT '图元素版本:维护更新 +1',
  created_at      DATETIME(6)  NOT NULL,
  updated_at      DATETIME(6)  NOT NULL,
  deleted_at      DATETIME(6)  NULL     COMMENT '软删时间;非 NULL 时 status 不再是有效状态',
  PRIMARY KEY (id),
  KEY ix_memory_events_owner (user_id, scope, status, start_at),
  KEY ix_memory_events_memory (memory_id),
  KEY ix_memory_events_dedupe (memory_id, normalized)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- memory_event_participants

-- 事件参与者 + 角色;未消歧时 entity_id 为 NULL,名称原文保留（角色缺失就是事实缺失,不能省略）。
CREATE TABLE memory_event_participants (
  id         BIGINT       NOT NULL AUTO_INCREMENT,
  event_id   CHAR(32)     NOT NULL COMMENT 'memory_events.id',
  user_id    CHAR(36)     NOT NULL,
  scope      VARCHAR(64)  NOT NULL COMMENT '作用域:与 memory_items.scope 同一口径',
  role       VARCHAR(64)  NOT NULL COMMENT '角色:subject/participant/companion/organizer/recipient…（通用,可扩展）',
  entity_id  CHAR(32)     NULL     COMMENT '消歧成功的参与者实体;未消歧为 NULL（名称在 name_text）',
  name_text  TEXT         NOT NULL COMMENT '参与者的原文名称 / 描述（未消歧也保留）',
  normalized CHAR(32)     NOT NULL COMMENT '名称规范化摘要（幂等去重用）',
  created_at DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_memory_event_participants (event_id, role, normalized),
  KEY ix_memory_event_participants_owner (user_id, scope, entity_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- memory_fact_links

-- 事实（关系 / 事件）↔ 来源绑定:绑定原始记忆的**有效版本**（revision）,保留轮次与原文摘录。
-- 一条记忆可映射多条事实;一条事实可有多个来源（不同轮次）。memory_revision 用于检出
-- 「绑定引用的版本已过期」（记忆正文更新后,对账据此复核事实是否仍被支持）。
CREATE TABLE memory_fact_links (
  id              BIGINT       NOT NULL AUTO_INCREMENT,
  fact_kind       VARCHAR(16)  NOT NULL COMMENT '事实类型:relation / event',
  fact_id         CHAR(32)     NOT NULL COMMENT 'memory_relations.id 或 memory_events.id',
  memory_id       CHAR(32)     NOT NULL COMMENT '来源记忆（memory_items.id）',
  memory_revision INT          NOT NULL COMMENT '绑定时的有效记忆版本（revision）:正文更新后据此复核',
  user_id         CHAR(36)     NOT NULL,
  scope           VARCHAR(64)  NOT NULL COMMENT '作用域:与 memory_items.scope 同一口径',
  turn_key        VARCHAR(160) NOT NULL COMMENT '来源轮次;空串 = 无轮次来源（用户手添 / 旧数据）',
  quote           TEXT         NOT NULL COMMENT '原始表述摘录（保留原文;可为空串）',
  meta            JSON         NOT NULL COMMENT '不确定性 / 解析证据（如 unresolved 指代说明）',
  generation      INT          NOT NULL COMMENT '写入时的记忆代次',
  created_at      DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_memory_fact_links (fact_kind, fact_id, memory_id, turn_key),
  KEY ix_memory_fact_links_memory (memory_id, fact_kind),
  KEY ix_memory_fact_links_owner (user_id, scope, fact_kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
