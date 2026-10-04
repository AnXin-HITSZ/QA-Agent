# 数据库迁移(调用日志与费用统计)

## 约定

- 文件名 `<四位版本>_<名称>.up.sql` / `.down.sql`，版本只增不复用。
- 已应用到任何环境的迁移文件不再修改，后续变更追加新版本。
- 迁移由人手动执行，应用启动不建表、不跑迁移框架：后端不引 Alembic，也绝不 `create_all`。
- 迁移账号与运行账号分离：迁移用有 DDL 权限的账号手动执行；运行账号只有 DML（`SELECT, INSERT, UPDATE, DELETE`），不持有 DDL。这条对应用连得上的**每个**库都适用，见下面「运行账号授权」。
- 库有两个：开发库 `qa_agent_dev`，生产库 `qa_agent_prod`。下面每条规则对这两个库一视同仁。
- 所有时间列存 UTC、`DATETIME(6)`；不用 `TIMESTAMP`，不设 `DEFAULT CURRENT_TIMESTAMP`；`NOT NULL` 列的默认值全部在 Python 侧给，建表语句里不写 `DEFAULT`。
- 不建外键：删文件 / 删索引任务 / 清缓存都不得级联删调用历史（技术方案 §4.5）。
- 每个 up 建的表，down 必须能删干净。

表结构与代码的对应关系：

- 模型（代码认为库长什么样）：`backend/app/metering/tables.py`；
- 建表 SQL（库实际长什么样）：本目录的 `*.up.sql`；
- 两边由 `tests/test_metering_migration.py` 离线逐项比对（列 / 类型 / 可空 / 自增 / 主键 / 唯一键 / 索引）。
  改表结构：先改模型，再新增一版迁移，然后跑该用例。

## 运行账号授权

建库、建账号的完整语句见 `docs/调用日志与费用统计技术方案.md` §9.2。应用账号 `qa_agent` 的 DML 授权要覆盖到每一个它连得上的库：

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON `qa_agent_dev`.*  TO `qa_agent`@`localhost`;
GRANT SELECT, INSERT, UPDATE, DELETE ON `qa_agent_prod`.* TO `qa_agent`@`localhost`;
```

`GRANT` 立即生效，不需要 `FLUSH PRIVILEGES`。给完用 `SHOW GRANTS FOR CURRENT_USER();` 核对。

只给 `SELECT` 的库上，写入会报 `1142 ... command denied`。注意这个错会把下面的问题盖住：同一张表上 `SELECT` 报 1146、`INSERT` 报 1142，因为 MySQL 在 `INSERT` 路径上先查权限、后查表存不存在。所以授权补齐之后才会露出 `1146 ... doesn't exist`，两个都修完才能写入。

## 执行

```sh
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u qa_migrate -p qa_agent_dev  < 0001_create_metering_tables.up.sql
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u qa_migrate -p qa_agent_prod < 0001_create_metering_tables.up.sql

# 回滚(连表带数据一起删掉，线上执行前先确认历史记录不再需要)
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u qa_migrate -p qa_agent_dev  < 0001_create_metering_tables.down.sql
```

- 开发库与生产库跑的是同一条命令，只换库名；连接串不出现在 SQL 文件里，主机 / 账号在命令行上给。
- `--default-character-set=utf8mb4` 不能省：文件里有中文列注释，客户端按默认字符集读会把它们读坏。
- 建表不显式指定 `COLLATE`，跟随库的默认排序规则（MySQL 8.0 是 `utf8mb4_0900_ai_ci`，5.7 是 `utf8mb4_unicode_ci`）——所以建库时就要选对（§9.2）。
- 迁移只执行一次：没有 `IF NOT EXISTS`，重复执行会报 `1050 ... table already exists`，这是故意的。

## 部署记录

| 版本 | 文件 | 开发库 | 生产库 |
| --- | --- | --- | --- |
| 0001 | `0001_create_metering_tables` | 待应用 | 待应用 |

## 0001：调用日志与费用统计四张表

`0001_create_metering_tables.up.sql` 建四张表：`call_events`（调用流水，一次真实外部请求一行）、`call_event_items`（批量请求按文件 / 页面的估算分摊）、`cache_events`（三层缓存命中统计，不含金额）、`price_config`（价格配置，运维写入）。字段口径见 `docs/调用日志与费用统计技术方案.md` §4，列语义写在建表语句的 `COMMENT` 里。

几点说明：

- **顺序：先应用迁移，再让带 `METERING_MYSQL_URL` 的代码上线。** 反过来也能活，但缺表期间写入会一直失败、事件转入补写目录（`METERING_PENDING_DIR`），`/summary.persistence.pending` 会显示积压——补写目录只是故障恢复队列，不是第二份日志库，积压久了占磁盘。
- **价格不随迁移种下。** `price_config` 建出来是空的，费用一律显示「无法估算」，由运维按官方价格页核实后手工 `INSERT`（技术方案 §5.1 有来源与填入示例）；迁移里不硬编码任何单价。
- **主键 / 唯一键就是幂等约束。** 补写重放靠它们天然去重（`INSERT ... ON DUPLICATE KEY UPDATE`，重复行不计入新插入），所以这几张表上不要另加会改变唯一性的键。
- **回滚会连数据一起删。** `call_events` 是审计线索（技术方案 §13），线上不要轻易执行 down。
