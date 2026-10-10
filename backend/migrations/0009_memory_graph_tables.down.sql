-- 0009 的回滚:把记忆图的七张表原样撤掉（只回滚结构,不还原数据）。
--
-- 注意:DROP TABLE 会**丢掉**这些表里的全部图数据（实体 / 别名 / 提及 / 关系 / 事件 /
-- 参与者 / 来源绑定）。它们不是不可再生的 —— 图是 MySQL 记忆的可重建投影,事实仍以
-- memory_items 为准;但回滚后需要代码也回到不使用图元素的版本,并且由 0009 建立的
-- 图提取产物不会自动重建（重新启用需按管理命令回填 / 重建）。
--
-- Neo4j 那边与本迁移无关:它是从这些表投影出来的,移除投影数据是管理命令的事,
-- 不在这份 SQL 里（本仓库的迁移只碰 MySQL）。

DROP TABLE IF EXISTS memory_fact_links;
DROP TABLE IF EXISTS memory_event_participants;
DROP TABLE IF EXISTS memory_events;
DROP TABLE IF EXISTS memory_relations;
DROP TABLE IF EXISTS memory_entity_mentions;
DROP TABLE IF EXISTS memory_entity_aliases;
DROP TABLE IF EXISTS memory_entities;
