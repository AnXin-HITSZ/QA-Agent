# 数据库迁移

本目录现在管三组表，都在这一个库里：

- **调用日志与费用统计**（0001 / 0002 / 0007，`docs/调用日志与费用统计技术方案.md`）；
- **认证、鉴权与用户管理**（0003，`docs/认证鉴权与用户管理技术方案.md`）；
- **长期记忆**（0004 + 0005，`docs/长期记忆系统技术方案.md`；0005 是同一组表的增量迁移，见文末）。

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

- 模型（代码认为库长什么样）：`backend/app/metering/tables.py`（计量）、`backend/app/auth/tables.py`（认证）、`backend/app/memory/tables.py`（长期记忆）；
- 建表 SQL（库实际长什么样）：本目录的 `*.up.sql`；
- 两边分别由 `tests/test_metering_migration.py` / `tests/test_auth_migration.py` / `tests/test_memory_migration.py` 离线逐项比对（列 / 类型 / 可空 / 自增 / 排序规则 / 主键 / 唯一键 / 索引）。
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
| 0004 | `0004_memory_tables` | 已应用 | 已应用 |
| 0005 | `0005_memory_scope_fencing_index_ops` | 已应用 | 已应用 |
| 0006 | `0006_memory_scope_state_history` | 已应用 | 已应用 |
| 0007 | `0007_widen_provider_columns` | 未应用 | 未应用 |

## 0001：调用日志与费用统计四张表

`0001_create_metering_tables.up.sql` 建四张表：`call_events`（调用流水，一次真实外部请求一行）、`call_event_items`（批量请求按文件 / 页面的估算分摊）、`cache_events`（三层缓存命中统计，不含金额）、`price_config`（价格配置，在「调用与费用 → 估算依据」界面维护）。字段口径见 `docs/调用日志与费用统计技术方案.md` §4，列语义写在建表语句的 `COMMENT` 里。

几点说明：

- **顺序：先应用迁移，再让带 `MYSQL_URL` 的代码上线。** 反过来也能活，但缺表期间写入会一直失败、事件转入补写目录（`METERING_PENDING_DIR`），`/summary.persistence.pending` 会显示积压——补写目录只是故障恢复队列，不是第二份日志库，积压久了占磁盘。
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

## 0004：长期记忆四张表

```sh
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u qa_migrate -p qa_agent_dev  < 0004_memory_tables.up.sql
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u qa_migrate -p qa_agent_prod < 0004_memory_tables.up.sql
```

建四张表：`memory_items`（记忆事实，按用户隔离、跨会话共享）、`memory_history`（变更审计，只追加）、`memory_jobs`（后台提取任务：幂等入队 + 租约认领 + 退避重试）、`memory_user_state`（每用户的记忆代次，见下）。字段口径见 `docs/长期记忆系统技术方案.md`「数据模型」，列语义写在建表语句的 `COMMENT` 里。

> 本节讲的是 0004 建出来的形态；`scope` / 认领凭证 / 阶段结果 / 版本化索引标记 / `memory_sources` / `memory_ops` 这些列与表由 **0005** 追加。**当前应用按三版升级，顺序 0004 → 0005 → 0006**（回滚逆序）。

几点说明：

- **MySQL 是事实源，Qdrant 只是可重建索引。** 记忆的向量集合（`MEMORY_COLLECTION`，默认 `user_memory`）任何时候都能从 `memory_items` 重建；库里不存向量，也不存「只在 Qdrant 有」的状态。删表重来只丢索引，不丢记忆。
- **`memory_items.content_hash` 上刻意没有唯一键。** 删除是软删（行留表里供审计）；如果 `(user_id, content_hash)` 唯一，用户删掉一条记忆后再遇到同样事实就永远写不回来。去重在「有效记忆」集合上做（`app/memory/repo.get_active_by_hash`）。
- **`memory_jobs.payload` 在任务进终态时清成 `{}`。** 对话正文只为「提取」这一件事在任务行上短暂停留，不在库里长留；`last_error` 只存脱敏摘要。
- **同一用户同一时刻只允许一个 `running` 任务**（认领查询显式避开租约未过期的用户）：自动维护要在「读到的现有记忆」上做 ADD/UPDATE/DELETE 决策，按用户串行才不会并发写出重复记忆。
- **`memory_user_state.generation` 是「彻底清除」的作废开关。** 用户点「清除全部记忆」时先给该用户的代次 +1、再删正文；`memory_jobs.generation` 记的是**入队时**的代次。任务执行前比一次、写入事务里（`repo.assert_generation`）再比一次，对不上就整批作废 —— 清除之前入队 / 已经开跑的任务，不会把刚被删掉的事实重新写回来。代次只增不减，也不随「软删一条」变化。
- **审计行在彻底清除时保留但正文脱敏**（`old_text` / `new_text` 置空、`reason` 改成「用户数据彻底删除」）：留一条「什么时候发生过一次删除」的线索，恢复 / 对账都不依赖已删正文。
- **顺序：先应用迁移，再让带长期记忆的代码上线。** 缺表期间记忆接口会明确报错（不会静默变成「没有记忆」）；聊天本身不受影响（记忆是软依赖）。
- **回滚会连记忆与审计一起删。** `0004_memory_tables.down.sql` 先删 `memory_user_state` 再删另外三张表（无外键，顺序只是习惯）；Qdrant 里的用户记忆集合不随之清理——那是本模块重建 / 清理流程的职责，执行前先让新版代码下线。**有 0005 时必须先回滚 0005 再回滚 0004**（0005 的列还在表上时 0004 的 down 也能删表，但按约定逆序执行，别留半截状态）。

## 0005：长期记忆的作用域、认领凭证、阶段结果与清理台账

```sh
# 先在开发库，再在生产库；两版一起上，顺序不能反
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u qa_migrate -p qa_agent_dev  < 0004_memory_tables.up.sql
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u qa_migrate -p qa_agent_dev  < 0005_memory_scope_fencing_index_ops.up.sql
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u qa_migrate -p qa_agent_prod < 0004_memory_tables.up.sql
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u qa_migrate -p qa_agent_prod < 0005_memory_scope_fencing_index_ops.up.sql
```

**只加不改**：给 `memory_items` / `memory_jobs` 各加几列（都是 `ADD COLUMN ... NULL` → `UPDATE` 回填 → `MODIFY ... NOT NULL`，本目录「NOT NULL 的默认值在 Python 侧给」的定式），另建两张新表。不改名、不删列、不删键，所以**已经跑过 0004 的库直接执行 0005 即可**。

| 落点 | 列 / 表 | 解决什么 |
| --- | --- | --- |
| 隔离 | `memory_items.scope`、`memory_jobs.scope`、`memory_ops.scope`、`memory_sources.scope` | 评测（`eval:<run-id>`）与正式（`''`）在同一套表、同一个 Qdrant 集合里严格分开：认领 / 补索引 / 清理 / 清除 / 检索全部按它过滤 |
| fencing | `memory_jobs.claim_token`、`memory_ops.claim_token` | 收尾 / 续租 / 阶段结果全部是「同时匹配 owner + 凭证」的条件更新；失去租约的执行者一行都改不动 |
| 阶段结果 | `memory_jobs.stages`、`outcome`、`committed_at` | 重试复用已付费的提取结果；`committed_at` 非空时重试只做索引收尾，不重复改事实 |
| 版本化索引标记 | `memory_items.meta_version`、`indexed_revision`、`indexed_meta_version`、`generation` | 正文变了才重新向量化；只改元数据走「刷新 payload」，零次 Embedding 调用；`generation` 让清除按代次上界删点 |
| 来源与时序 | `memory_items.kind`、`event_time`；新表 `memory_sources` | 事实类型 / 事件时间与记录时间分开；记忆 ↔ 来源会话与轮次（`turn_id` 是入队幂等的稳定标识，替代正文哈希） |
| 清理台账 | 新表 `memory_ops` | 删除 / 彻底清除时**先在事实事务里登记**，再在事务外清 Qdrant；失败按退避重试，不是「写一行日志」 |

几点说明：

- **为什么不直接改 0004：** 本目录的约定是「已应用的迁移文件不再修改」。把上面这些塞回 0004，已经跑过 0004 的库就没有升级路径了（重跑会撞 `1050 table already exists`）；增量迁移让「跑过 0004 的库」和「全新库」走同一条路：先 0004，再 0005。
- **两张新表的主键形态不同是有意的：** `memory_sources` 用 `AUTO_INCREMENT BIGINT`（同一条记忆有多个来源是常态，键只用来去重与定位）；`memory_ops` 用 `CHAR(32)`（与 `memory_jobs` 一致，`uuid4().hex`，操作 id 会被外部引用）。
- **`memory_sources` 的唯一键 `(memory_id, turn_id)`** 是入队幂等的落点：同一轮对话重复入队只记一条来源；同一句话在不同会话里说过会记成两条来源（正文哈希做不到这一点）。
- **`memory_ops` 上不放外键**（本目录一律不建外键）：彻底清除时先删记忆行、再由台账去清向量点，「删除操作行的引用」不该反过来拦住记忆行的删除。
- **回滚顺序：先 0005 再 0004。** `0005_..._ops.down.sql` 只删表与列——它**丢掉这些列里的值**（评测数据的 `scope`、认领凭证、阶段结果、索引版本标记都随之消失），而且不做事前校验。所以回滚前先确认：代码已回到读不到这些列的那一版，且没有正在跑的 Worker。
- **up 已于 2026-10-08 在开发库与生产库按 0004 → 0005 → 0006 执行**（见部署记录）；**逆序回滚仍未在任何真库上演练过**。回滚演练去可弃的库上做，见 `docs/长期记忆系统部署与评测指南.md` §7 与 `tests/test_memory_mysql.py`（设 `MEMORY_TEST_MYSQL_URL` 后会自动按 0004 → 0005 → 0006 应用、逆序回滚）。


## 0006：记忆审计与清除代次按作用域隔离

文件：`0006_memory_scope_state_history.up.sql` / `.down.sql`。

依赖 0004、0005；已执行 0005 的库只需新增执行 0006。迁移新增 `memory_history.scope`，把 `memory_user_state` 主键改为 `(user_id, scope)`，任务幂等唯一键加入 scope。停止旧 Worker 并备份后手工执行；up 已于 2026-10-08 在开发库与生产库执行（见部署记录），逆序回滚未在真库演练过。

```sh
mysql --default-character-set=utf8mb4 -h <host> -u qa_migrate -p <database> < backend/migrations/0006_memory_scope_state_history.up.sql
```

全新库按 0004 → 0005 → 0006；回滚按 0006 → 0005 → 0004。回滚前清理评测数据，避免恢复旧任务唯一键冲突。回滚会丢失独立评测代次与审计作用域，不是无损操作。历史已清除事实的审计无法可靠回填评测作用域，升级前应清理旧评测资料并核对备份。

`tests/test_memory_mysql.py` 按三个迁移顺序执行；需配置可弃测试库 `MEMORY_TEST_MYSQL_URL` 才能验证真实迁移与行锁。


## 0007：放宽 provider 列（call_events / price_config）

`0007_widen_provider_columns.up.sql` 做两条 `ALTER TABLE ... MODIFY`：两张表的 `provider` 由 `VARCHAR(32)` 放宽到 `VARCHAR(255)`，与同表 `endpoint` 同宽（provider 取值就是端点主机名，DNS 上限 253 字符）。

背景（2026-10-08 生产实测）：rerank 接到新网关 `llm-hh23ndoe58qsq2rn.cn-beijing.maas.aliyuncs.com`（49 字符），超 `VARCHAR(32)` → MySQL 严格模式整批 INSERT 报 `1406 Data too long for column 'provider'`；凡是与 rerank 事件同批的记录（含正常的 embedding / llm）都进不了库，60 条全部落入补写目录、反复重试 20+ 小时进不去（库本身健康）。`price_config.provider` 与 `call_events.provider` 同一取值来源，不一起放宽则长主机名的价目同样撞 1406、永远配置不进去。

```sh
# 先在开发库，再在生产库（两库跑的是同一条命令，只换库名）
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u qa_migrate -p qa_agent_dev  < 0007_widen_provider_columns.up.sql
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u qa_migrate -p qa_agent_prod < 0007_widen_provider_columns.up.sql
```

几点说明：

- **只改列宽，不改名 / 不删列 / 不删键**：已经建过表的库直接执行 0007 即可，不需要重跑 0001。
- **应用后补写自动收干，不用手工处理补写目录**：积压文件按 `event_id` 主键幂等重放，下一轮补写就会入库并删除文件；DDL 立即对新 INSERT 生效，**不需要为此重启应用**（运行时不校验列宽）。
- **回滚**：`0007_widen_provider_columns.down.sql` 把两列改回 `VARCHAR(32)`；只在确认表里没有超宽 provider 的库上执行，否则严格模式直接 1406（与 0002 的 down 同理），生产库应用过 0007 后不要回滚。
- **真实宽度回归**：`tests/test_metering_mysql.py::test_long_provider_round_trip` 用 49 字符的生产主机名在真库上验证进出（SQLite 不校验长度，只有真库挡得住——0002 的教训）。
# 0008：事件时间与消息来源

在 0001–0007 已完成的库上执行 `0008_memory_fact_context.up.sql`。迁移新增 `memory_items.fact_context JSON NOT NULL`，存量行回填空对象；同时新增可空的 `memory_history.old_context/new_context`。不从入库时间回填事件时间。

先停止旧 Worker 并备份，在开发库演练后再升级应用。应用不自动执行迁移。本轮尚未在真实 MySQL 执行 0008。

`0008_memory_fact_context.down.sql` 会丢失时间精度、状态和消息级来源，以及对应审计快照，回滚前备份；代码与表结构必须一起回滚。

详细行为、隔离及复测命令见 [记忆事件与时间改进及复测说明](../../docs/记忆事件与时间改进及复测说明.md)。
