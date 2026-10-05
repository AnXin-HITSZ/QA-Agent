# 数据库迁移

本目录现在管两组表，都在这一个库里：

- **调用日志与费用统计**（0001 / 0002，`docs/调用日志与费用统计技术方案.md`）；
- **认证、鉴权与用户管理**（0003，`docs/认证鉴权与用户管理技术方案.md`）。

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

- 模型（代码认为库长什么样）：`backend/app/metering/tables.py`（计量）、`backend/app/auth/tables.py`（认证）；
- 建表 SQL（库实际长什么样）：本目录的 `*.up.sql`；
- 两边分别由 `tests/test_metering_migration.py` / `tests/test_auth_migration.py` 离线逐项比对（列 / 类型 / 可空 / 自增 / 排序规则 / 主键 / 唯一键 / 索引）。
  改表结构：先改模型，再新增一版迁移，然后跑对应用例。

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
| 0001 | `0001_create_metering_tables` | 已应用 | 已应用 |
| 0002 | `0002_widen_metering_identifiers` | 已应用 | 已应用 |
| 0003 | `0003_auth_and_conversations` | 已应用 | 已应用 |

## 0001：调用日志与费用统计四张表

`0001_create_metering_tables.up.sql` 建四张表：`call_events`（调用流水，一次真实外部请求一行）、`call_event_items`（批量请求按文件 / 页面的估算分摊）、`cache_events`（三层缓存命中统计，不含金额）、`price_config`（价格配置，在「调用与费用 → 估算依据」界面维护）。字段口径见 `docs/调用日志与费用统计技术方案.md` §4，列语义写在建表语句的 `COMMENT` 里。

几点说明：

- **顺序：先应用迁移，再让带 `METERING_MYSQL_URL` 的代码上线。** 反过来也能活，但缺表期间写入会一直失败、事件转入补写目录（`METERING_PENDING_DIR`），`/summary.persistence.pending` 会显示积压——补写目录只是故障恢复队列，不是第二份日志库，积压久了占磁盘。
- **价格不随迁移种下。** `price_config` 建出来是空的，费用一律显示「无法估算」，由运维按官方价格页核实后在前端「调用与费用 → 估算依据」里填写（只增 + 删；技术方案 §5.1 有来源、SQL 示例与只增语义说明）；迁移里不硬编码任何单价。
- **主键 / 唯一键就是幂等约束。** 补写重放靠它们天然去重（`INSERT ... ON DUPLICATE KEY UPDATE`，重复行不计入新插入），所以这几张表上不要另加会改变唯一性的键。
- **回滚会连数据一起删。** `call_events` 是审计线索（技术方案 §13），线上不要轻易执行 down。

## 0002：放宽标识列（document_id / item_key）

`0002_widen_metering_identifiers.up.sql` 只做四条 `ALTER TABLE ... MODIFY`：`call_events`、`call_event_items`、`cache_events` 的 `document_id` 由 `CHAR(32)` 放宽到 `CHAR(36)`，`call_event_items.item_key` 由 `VARCHAR(320)` 放宽到 `VARCHAR(549)`。

- **为什么：** `document_id` 不是本模块 `event_id` 那种 32 位 hex，而是 `app/rag/documents.py` 登记的 `str(uuid.uuid4())`——带连字符的 36 位。`CHAR(32)` 装不下，MySQL 严格模式下整批 `INSERT` 报 `1406 Data too long`，带 `document_id` 的索引类事件一条都进不了库，全积压在补写目录里反复重试（2026-10-04 生产实测）。
- **`item_key` 同一个原因：** 它是 `document_id + ":" + oss_key`，最长 36 + 1 + 512 = 549；按常见路径估成 320 就会在长路径上再撞一次 `1406`。
- **为什么本地测试没拦住：** 本地跑 SQLite，而 SQLite 不校验 `CHAR` / `VARCHAR` 长度。现在有两道新的看护：`tests/test_metering_store.py::test_identifiers_fit_the_columns_that_store_them`（按各列上限算最长标识再比列宽）与 `tests/test_metering_mysql.py::test_long_identifiers_round_trip`（真库上写 36 位 id + 顶格 oss_key）。
- **回滚：** down 会把列宽改回去，只适用于空库 / 开发库——表里只要有 36 位的 `document_id`，严格模式下这条 `ALTER` 就会报 `1406`。

## 0003：认证、鉴权与用户管理六张表

```sh
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u qa_migrate -p qa_agent_dev  < 0003_auth_and_conversations.up.sql
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u qa_migrate -p qa_agent_prod < 0003_auth_and_conversations.up.sql
```

建六张表：`users`（账号；注册一律 `user` 角色，管理员由命令行建立）、`auth_sessions`（一行 = 一次登录 = 一个 refresh 令牌家族的锚点，撤销按 `revoked_at` 标记不删行）、`auth_refresh_tokens`（轮换链：每次刷新新增一行、旧行标 `consumed_at`，只存 SHA-256 摘要）、`email_tokens`（邮箱验证 / 重置密码共用一张表，`purpose` 区分用途、互不通用）、`conversations`（会话目录**与归属凭据**：过期只清行，`deleted` 是墓碑）、`auth_audit`（注册 / 审批 / 禁用 / 登录失败 / 重放检测等，只追加）。字段口径见 `docs/认证鉴权与用户管理技术方案.md` §7，列语义写在建表语句的 `COMMENT` 里。

几点说明：

- **`users.email` 显式 `COLLATE utf8mb4_bin`**，是本目录里唯一不跟随库默认排序规则的列。邮箱规范化只做「去首尾空白 + 整体小写」（`app/auth/emails.py`），不按提供商合并点号、不删 `+` 标签；唯一键必须按精确字符串相等判定——跟随库默认的 `utf8mb4_0900_ai_ci` 会把 `resume@x.com` 与 `résumé@x.com` 判成同一个地址（它不区分重音），两个账号会撞唯一键、查询互相串号。**这一条本地测不出来**：SQLite 上只是注册了一个同名的模拟 collation（`tests/conftest.py::register_sqlite_collations`），真库上的排序规则由 `tests/test_auth_mysql.py::test_email_column_collation_is_binary` 验证（需设 `AUTH_TEST_MYSQL_URL`，否则该文件整体跳过）。
- **`conversations.thread_id` 唯一且列宽 160。** 线程 id 形状 `qa:{env}:chat:v1:u:{user_id}:c:{id}`（env 进 id 是为了同一台 Redis 上多环境天然隔离），比 36 位 UUID 长得多；按 `CHAR(32)` 那样估会重演 0002 的 `1406`，所以 `tests/test_auth_mysql.py` 有一条真长度的往返用例。
- **顺序：先应用这一版，再让认证代码上线。** 缺表期间注册 / 登录 / 会话列表直接失败（认证是硬依赖，不会降级成「匿名可用」）；反过来先上代码也不会写坏数据，只是认证接口一直报错。
- **首个管理员不随迁移种下。** 迁移里不硬编码任何账号、密码或哈希（也不种价格行那种「示例数据」）；第一个管理员由运维在服务器上用 `python -m scripts.create_admin` 建立（交互式输入密码，记审计），见技术方案 §8。
- **本版之前的旧会话只有 Redis 线程、没有目录行。** 升级后它们不出现在「历史对话」列表里（列表按 `conversations` 分页，不扫 Redis）；是否要补目录行由运维按技术方案 §7 决定，迁移不猜数据。
- **回滚会连账号与审计一起删。** `0003_auth_and_conversations.down.sql` 删掉六张表（顺序与建表相反，无外键所以顺序不强制）；Redis 里的对话 checkpoint 不随之清理——那是会话删除流程的职责。执行前先让新版代码下线。
