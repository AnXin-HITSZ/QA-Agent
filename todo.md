# QA-Agent 待办 & 设计固化

## 新增待办（2026-10-03）

- [ ] **缓存引用图**：基于文件缓存清单构建缓存反向引用，删除或更新文件时识别仍被其他文件使用的缓存，避免清理共享结果后重复付费。复用 `document_id`、清单及 `schema_version`，按需补充计算依赖；当前不实施，继续使用已确认的清单差集清理方案。
- [ ] **文件删除任务恢复**：将现有“缓存清理恢复”扩展为完整删除任务恢复。删除原件前持久化恢复记录，跟踪 OSS 原件、Qdrant 索引、缓存及文件登记的删除阶段；支持幂等重试、启动恢复及暂时性故障后的重试，使用文件身份与版本校验防止旧任务误处理同路径新文件。全部完成后才清理恢复记录，并补充断连、中断和重新上传测试。当前基础实现尚未覆盖完整流程，本项保持未完成。

> 本文件记录已确认的设计与"动手前待确认项",便于取到文件后接着落实。
> 约定:一步一步来,不使用工作流。

---

## 调用日志与费用统计(MySQL)—— ✅ DONE(2026-10-03,待 ECS 建库 + 真实调用验收)

> 按 [docs/调用日志与费用统计技术方案.md](docs/调用日志与费用统计技术方案.md) 实现:记录 embedding / OCR 的
> **真实外部调用**与按配置单价的**估算费用**。日志主存储 MySQL(Redis 仍只做缓存 + 会话,Qdrant 仍只做向量),
> **不含聊天 LLM**,**不接官方账单**,界面只写「估算费用」。

- **库表(4 张,无外键,`DATETIME(6)` / `DECIMAL`,`InnoDB` + `utf8mb4`)** —— [tables.py](backend/app/metering/tables.py) +
  [迁移 0001](backend/migrations/0001_create_metering_tables.up.sql)(纯 SQL、手工执行,约定见
  [migrations/README.md](backend/migrations/README.md)):`call_events`(一次真实请求一行,
  每次重试各一行,`call_group`/`attempt_no`/`retry_of` 串链路,`http_attempts` 记 SDK 内部重试)、
  `call_event_items`(批量请求按文本数比例**分摊**,整笔金额只记一次)、`cache_events`(三层缓存命中 / 未命中 / 复用 / 去重,
  与费用分表不重复计)、`price_config`(维度 = 服务 / 供应商 / 模型或 OCR Type / 计费单位 / 币种 + `effective_from`,
  运维写入)。**应用绝不 `create_all`,也不引迁移框架:表只由 migrations 下的 SQL 演进**;
  运行账号只给 DML,迁移账号另建。
- **口径** —— 超时 ≠ 免费(用量 / 金额留空 + `billing_status=unknown`,不记 0);用量优先供应商回报,
  其次本端计数,**绝不按字数折算 token**;缺价 / 缺用量显示「无法估算」+ 原因;全链路 `Decimal`,
  不同币种 / 单位不合并;事件发生时对命中的价目做**不可变快照**,改价不重算历史;
  文档正文 / 向量 / 图片 / 完整查询文本 / 密钥 / 签名 URL 一律不落库(endpoint 只留主机名,错误摘要过 `redact`)。
- **埋点(不改变现有缓存行为)** —— [metered_embeddings.py](backend/app/rag/metered_embeddings.py) 包住 embedding 客户端
  (批量 / 部分命中 / 去重 / 查询向量只计实际未命中)、[ocr.py](backend/app/rag/ocr.py) 每次真实识别请求记一条
  (重试逐次、失败 / 超时如实记)、`ocr_cache.py` / `embedding_cache.py` 追加一行缓存统计上报(键、TTL、单飞逻辑未动)。
- **故障补写(不影响业务)** —— [writer.py](backend/app/metering/writer.py) + [pending.py](backend/app/metering/pending.py):
  内存队列 → 后台线程批量 INSERT → 失败落 `METERING_PENDING_DIR` → 原子领取 + `event_id` 主键幂等补写 + 指数退避,
  提交成功才删补写文件;**写库失败不会把已成功的 OCR / Embedding 变成业务失败,补写绝不重做付费调用**;
  连补写文件都写不下才计 `lost` 并告警(接口 / 前端都会暴露 pending / lost / db_ok)。崩溃窗口如实写进方案 §11。
- **接口(`/api/v1/admin/metering`)** —— [metering.py](backend/app/api/routes/metering.py):`GET /calls`(分页 + 过滤,
  缺省最近 7 天、上限 366 天 / 200 条一页)、`GET /calls/{event_id}`(含分摊与价格快照)、`GET /summary`(聚合在 SQL 里做)、
  `GET /prices`。**价目写接口(只增 + 删)**:`POST /prices`(重复唯一键 409,写入后价格表缓存立即失效)、
  `DELETE /prices/{id}`(204 / 404;历史事件按快照估算,不重算)。日志仍只读。
  **权限缺口如实暴露**:后端无登录态,接口只应在受信网络内暴露,统一鉴权机制后续引入(2026-10-04
  已按决策移除早前的可选 `ADMIN_API_TOKEN` 单点令牌)。
- **前端「调用与费用」tab** —— [MeteringView.vue](frontend/src/components/MeteringView.vue) +
  [MeteringDetail.vue](frontend/src/components/MeteringDetail.vue) + [useMetering.ts](frontend/src/composables/useMetering.ts) +
  [metering.css](frontend/src/styles/metering.css);[App.vue](frontend/src/App.vue) / [AppHeader.vue](frontend/src/components/AppHeader.vue)
  加 `#/metering` 视图与折页标签。筛选(时间范围 / 服务 / 用途 / 状态 —— 胶囊按钮点选即生效,
  样式对齐「待办清单」;自定义范围用日期选择器 + `HH:mm` 时刻,「应用」提交)、概览卡(实际调用 / 成功失败 / 缓存命中 / 估算费用
  按币种分列)、分组面板(按服务、用量与费用、缓存三层、按天纯 CSS 条)、分页表格、详情抽屉(用量口径 / 价格依据 /
  错误 / 重试链路,文件可跳知识库)、加载 / 空 / 错误 / 缺价 / 日志不完整各有独立文案。
  **「估算依据」内可直接维护价目**(2026-10-04):「＋ 添加价目」表单(服务 / 计费单位胶囊,字段校验后才提交;
  本地生效时间转 UTC)+ 表格内行内确认删除 —— 只增 + 删,改价 = 追加更晚生效的规则;停用 / 编辑留到以后。
  无原生 `<select>` / `datetime-local`:日期选择器为共享组件 [DateField.vue](frontend/src/components/DateField.vue)
  (自 TodoDateField 抽出)。`npm run build`(vue-tsc)通过。
- **测试** —— `test_metering_{model,calls,writer,api,migration}.py` 全离线(mock 付费调用;迁移用例把
  `migrations/*.sql` 解析成结构再与模型逐项比对);
  [test_metering_mysql.py](backend/tests/test_metering_mysql.py) 是 MySQL 专属集成项(执行迁移 SQL 建表 / 回滚 /
  列类型精度 / 索引 / 无外键 / Decimal 往返 / 微秒与 UTC 边界 / 并发幂等写 / 真实写出器落盘补写),
  **未设 `METERING_TEST_MYSQL_URL` 时整组 skip**,且拒绝在不像测试库的库名上建表 / 删表。**SQLite 通过 ≠ MySQL 通过**。
- ⏭ **待你执行 / 未验证**:① ECS 上建 `qa_agent_dev` / `qa_agent_prod` 两库并执行
  `mysql <db> < migrations/0001_create_metering_tables.up.sql`(语句见方案 §9.2/§9.3 与 migrations/README.md);
  ② 按官方价格页核实后在前端「调用与费用 → 估算依据」里填入单价(不内置、不硬编码;为空时界面显示「无法估算」);
  ③ 配置 `METERING_MYSQL_URL` 后做一次真实 OCR / 索引小样本验收(真实计费仍未验证)。

---

## RAG 知识库(Step 2)—— 进行中

> **架构**:原件存**阿里云 OSS**(私有桶,唯一事实源,抗 ECS 重建);ECS 的 **Qdrant** 只放向量 + 元数据;引用来源用短时效**签名 URL** 指回原件。
> **分类 = 用户在前端手建的 OSS 前缀节点**(人工放置,不再按关键词猜);用户**只能上传散文件**到某节点(平铺 `前缀+文件名`),不能上传目录树。

### 已确认的关键决定

- **Embeddings**:`qwen3.7-text-embedding-flash`(默认 1024 维,OpenAI 兼容端点,2026-10-04 自 `text-embedding-v4` 换入;换模型必重建索引,`EMBEDDINGS_DIM` 与版本号同步)。限制:单请求 ≤ 20 条输入(代码保守按 10 分批)、单条 ≤ 128K token ⊃ 现切块尺寸。
- **涉密红线**:已确认切块原文可发 DashScope 云 + 向量存云 ECS Qdrant + 扫描件 OCR 正文也可上云 → 维持云方案,不做本地 embedding / 本地 Qdrant。
- **原件仓库**:阿里云 OSS 私有桶 `anxin-hitsz-qa-agent`(region cn-shenzhen,prefix `knowledge/`)+ SSE 服务端加密 + RAM 最小权限;AccessKey 只进 `backend/.env`,**绝不提交**。
- **发票 / 报销**:文件名已编码金额(`发票6100`)、目录名已编码费用大类+实际支出+预算上限 → 报销/审计问答可直接解析文件名算账;`metadata.py` 富化字段与分类**正交、非权威**。
- **抽取器分派**:按文件扩展名(见 2c);PDF 运行时再判电子版 / 扫描件。

### 步骤与进度(一步一步;不写测试进生产代码,只做独立自检)

- **2a OSS 接入层** ✅ DONE(2026-09-19)—— `app/rag/oss.py`(oss2 薄封装:put/get/list/sign/delete + 目录标记 + `x-oss-meta-*` 下划线↔连字符对称转换)。真桶读写往返 + 私有桶签名下载自检全绿。
- **2b 知识库树端点** ✅ DONE(2026-09-19)—— `app/api/routes/knowledge.py` + `app/schemas/knowledge.py`,挂 `/api/v1/knowledge`。5 操作:`GET /tree`、`POST /folder`、`DELETE /folder`(空 prefix→400 防清库)、`POST /upload`(多文件平铺,重名 `skipped_exists` 不覆盖,逐文件返回结果)、`DELETE /object`。处理器全用**同步 `def`**(FastAPI 丢线程池,oss2 阻塞调用不堵流式对话)。真桶 12 断言 HTTP 自检全绿。
- **2c 抽取器 + 富化** ✅ DONE(2026-09-19)—— `app/rag/extract.py`(按扩展名抽文本:pdf/docx/xlsx/pptx/txt/xls;扫描件与图片标 `needs_ocr`;坏文件不抛异常、只标 `error`,整批不中断)+ `app/rag/metadata.py`(纯正则从路径+文件名解析 project/person/fee_category/spent/budget/invoice_amount/doc_type)。独立自检全绿。
- **2d OCR 接入** ✅ DONE(2026-10-03)—— 实际做法与当初"在 ocr.py 里补 `_ocr_aliyun` 占位"的设想不同,按 [docs/OCR接入技术方案.md](docs/OCR接入技术方案.md) 重写为完整四层:`ocr.py`(阿里云「OCR 统一识别」RecognizeAllText / ocr-api 2021-07-07 / RPC V1 签名 / Type=Advanced·Invoice·MixedInvoice·PaymentRecord + 响应归一化)、`document_extract.py`(逐页提取:pypdfium2 渲染 PDF 每页 → 空白页判定 → 识别 → 带页码的文本单元;**四种提取方式由用户手选**,不自动分类 / 不自动切换)、`ocr_cache.py`(当时为 内容哈希+页码+DPI+引擎+类型 的本地缓存,**失败不写缓存**,`refresh_ocr` 可绕过;🔁 2026-10-03 该缓存已由 2f 改为 Redis 两层缓存,页码与整份哈希不再进识别键)、`ocr_jobs.py`(索引任务:单工作者锁 + 心跳 + 明细 jsonl + 重启对账)。原 `_resolve_text` / `_ENGINES` / `_IMPLEMENTED` 占位机制已删除。凭证独立:`OCR_ENGINE=aliyun` + `OCR_ALIYUN_ACCESS_KEY_ID/SECRET/ENDPOINT`(RAM 子账号,与 OSS / DashScope 三套分开)。**真实阿里云调用与计费尚未验证**,需先配凭证做小样本验收(方案 §13)。
  - 🔁 **(2026-10-04)** 通用文字识别拆为两档,前端「提取方式」五选一:`general` = 基础版(`Type=General`,便宜,清晰扫描件够用)、`general_advanced` = 高精版(`Type=Advanced`,复杂背景 / 倾斜 / 印章更稳)。价目表按 target 分档各配一行(或留空用 ocr 通用价)。
- **2e 摄取管线 + reindex** ✅ DONE(2026-09-19)—— `app/rag/ingest.py`:`reindex(prefix="")` 遍历 OSS(`list_all`)→ 逐文件 `get_object` 下载 → `extract` 抽文本 →(`needs_ocr`:OCR 未接入则跳过告警;接入后仅图片直接 OCR,扫描 PDF 待 2d 渲染)→ `_pack` 切块(目标 1000 字/块、超长块带 150 重叠硬切)→ `parse_metadata` 富化 payload(`text/oss_key/source/category=手建前缀/chunk_index/ext` + project/person/fee_category/...)→ 分批 embedding(`chunk_size=10`,≤10/请求)→ `store.upsert`(每批 128)。point id = `uuid5(NS,"<key>#<i>")` 确定性(便于将来增量)。单文件异常/坏文件优雅降级(跳过记原因、不中断整批)。`store.py` 加 `recreate_collection()`+`make_point()`;`embeddings.py` 加 `chunk_size=10`。端点 `POST /api/v1/admin/knowledge/reindex`(`admin_router`,同步 `def` 走线程池;先验依赖再清库,OSS/Qdrant/Embeddings 缺 → 503)。v1 = 清空 + 全量重建;增量(hash/mtime)后置。独立自检 30 断言全绿(纯逻辑 + 全 mock 端到端,不触网、不进 tests/)。
  - 🔁 **(2026-10-03)** `reindex()` / `index_file()` 两个同步包装已删除,`ingest.py` 只剩 `run_index_job(kb, scope, opts)` 一条编排(固定源清单 → 建候选版本集合 → 复制范围外旧向量 → 逐文件写入 → 校验 → 发布指针),`store.recreate_collection()` 不再被引用;端点由 `POST /admin/knowledge/reindex` 换成 `POST /admin/knowledge/index-jobs`(202 + 轮询),另有 `GET /index-manifest`(当前 / 可回退版本)与 `POST /index-manifest/rollback`。
- **2f Redis 三层缓存 + 文件身份** ✅ DONE(2026-10-03,离线可验证部分)—— 按 [docs/Redis缓存与文件身份管理技术方案.md](docs/Redis缓存与文件身份管理技术方案.md) 落地:`cache_store.py`(Redis / Memory / Null 后端 + TTL 令牌锁 + 单飞 + 写重试;`CacheError` 继承 `RuntimeError` → 路由自动 503;OOM 有固定中文文案)、`ocr_cache.py` 重写为两层(识别键只含图片字节 + 调用参数,不含页码与整份文件哈希;文本层按内容哈希 + `TEXT_RULES_VERSION`,改规则只重转换不重 OCR)、`embedding_cache.py`(切块复用向量,🔁 检索问题已不再走缓存,见下),`documents.py`(UUID v4 身份 + 路径映射 + 正式 / 待发布清单 + 发布后按「旧清单键 − 新清单键」清理 + 删除快照与启动对账),并接入 `ingest` / `ocr_jobs` / `knowledge` 路由 / `retrieve`。`.env` 新增 `CACHE_BACKEND`(`redis`/`memory`/`none`)、`CACHE_*`、`EMBEDDINGS_VERSION`;部署侧要求 `maxmemory` + `noeviction` + AOF(应用不改共享 Redis 全局配置)。**未验证**:真实 Redis 联调与容量表现、真实 OCR 计费。旧本地缓存不迁移(旧键缺图片哈希,无法验证输入对应关系,见方案 §11)。
  - 🔁 **(2026-10-03) 代码复检修复 4 项**(先复现后修,离线测试全绿):① 发布恢复逐文件比对「当前清单 vs 记录快照」—— 过期任务不覆盖更新的清单、也不因全库当前版本已变就跳过自己那几个文件的收尾;写清单与清缓存两阶段分开落盘,重复恢复幂等。② 删除改为**先落恢复记录再删原件**,四阶段(原件 / 向量 / 缓存 / 登记)各自落盘;Redis 读不到登记 / 清单时不删原件、直接 503;清理只用删除时的快照,同路径重传的新文件(新 `document_id`)不受延迟执行的旧删除影响。③ 等锁超时不再绕过锁计算:抛 `CacheBusy`(可重试,HTTP 503);批量向量化先去重,限时等待内重取锁并复查缓存,持锁期间多把锁一起续期。④ 查询向量不再走缓存,检索不依赖 Redis(索引侧仍用 embedding 缓存)。
- **3 检索工具 + 引用来源** ✅ DONE(2026-09-19)—— `app/rag/retrieve.py`:`search_knowledge(query, top_k=5, score_threshold=0.2)`(**v1 纯语义、全库、不带过滤** —— `store.search` 一行未改)embed_query → 检索 → 丢弃低于阈值 / 空正文 → 结构化命中(text/oss_key/source/category/score/chunk_index/ext)+ `format_hits` 拼给 LLM 的编号片段。`tools.py` 加 `@tool(response_format="content_and_artifact") search_knowledge`:返回 `(给 LLM 的文本, hits artifact)`,未配置 / 零命中优雅降级,进 `TOOLS`(agent 自主决定何时调,**不加预检索节点**、图结构不变 `START→agent⇄tools→END`)。**(2026-09-20 加固)** 降级 `except` 由只兜 `RuntimeError` 放宽到 `except Exception`——原先兜不住 embedding/向量供应商的 HTTP 错误(如 DashScope 欠费 `openai.BadRequestError 400 Arrearage`),会穿透 ToolNode 把整条 SSE 流打崩 500;现在任何检索失败都降级为「知识库暂时无法访问、据常识回答」,详细异常写 `logger.warning` 便于排查,只回 LLM 一句干净提示(不把冗长报错喂给模型)。`trace.py` 加 `used_sources(messages, top_n=5)`:读 `ToolMessage.artifact`、**限定最后一个 HumanMessage 之后(本轮)**、按 `oss_key` 去重(留最高分)、按分降序 —— 避开 checkpointer 全量历史里的旧来源。`nodes.py` SYSTEM_PROMPT 微调一句(日常答疑先查库、查不到再据常识并说明)。`schemas/chat.py` 加 `SourceCitation`(oss_key/source/category/score/url)+ `ChatResponse.sources`(默认空、非破坏)。`chat.py` 加 `_sign_sources`(**emit 时**用 `oss.sign_url` 现签 15min URL、不存库;签名失败 url 留空仍返回元信息);`/chat` 回填 `sources`,`/chat/stream` 把 tools 节点消息并入 `seen` + `done{skill, sources}`。独立自检(全 mock embeddings/store/sign_url,不触网、不进 tests/)全绿。**score_threshold=0.2 为经验默认,需按真实语料再调。**
- **4a 参考来源展示** ✅ DONE(2026-09-19)—— 消费 `ChatResponse.sources` / SSE `done` 事件的 `sources`,答案末尾**常驻列表**(不折叠)。`api.ts` 加 `Source` 接口 + `ChatResponse.sources` + `StreamHandlers.onDone(skill, sources)` + `done` 分派解析 sources;`useChat.ts` 的 `Msg.sources`、`reply` 初始 `sources:[]`、`onDone` 回填 `reply.sources`;`AnswerRecord.vue` 加 `sources` prop + `srcName`(优先文件名、退取 oss_key 末段)+ `<footer class="ar__sources">` 常驻清单(`s.url` 可点 `target=_blank rel=noopener`,url 为 null 显示「链接不可用」不可点;类别灰字 + 极淡分数)+ 发丝线分隔的 scoped CSS;`App.vue` 传 `:sources="m.sources"`。历史回放(openConversation)不带 sources → 回放答案不显来源(一致)。`npm run build` 通过。
- **4b 前端知识库管理视图 + 增量索引**(拆 3 小步;导航 = 面包屑 + 单层列表)
  - **4b-1 后端增量索引** ✅ DONE(2026-09-19)—— 新需求:**上传即建索引、删文件即删索引**(不再只靠全量重建)。`store.py` 加 `delete_by_oss_key(key)`(按 payload `oss_key` 精确匹配删点,collection 不存在→0,best-effort)+ `indexed_keys(prefix)`(按 `category==prefix` 分页 scroll 拿已索引 key 集合,供徽标);`ingest.py` 抽出共享 `_file_points(key,res,text)`(切块+payload+确定性 id,reindex 与增量共用),加 `index_file(key)`(先验 embeddings → 先删旧点(幂等)→ 抽取/切块/向量化/upsert;needs_ocr/unsupported/空→indexed=False+reason 仍返回;运行时错不外抛只记 reason;Embeddings/Qdrant 未配→抛 RuntimeError 交路由转 503)+ `delete_file_index(key)`;`schemas/knowledge.py` 加 `IndexFileRequest/IndexFileResult/IndexedKeysResult`;`knowledge.py` 加 logger、`_oss_call`→`_dep_503`(广涵 OSS/Qdrant/Embeddings)、`_drop_vectors`(删原件/节点后 best-effort 连带清向量,失败只告警不阻断),`delete_file`/`delete_folder`(删前先列 key 再逐个清向量)接线,新增 `POST /admin/knowledge/index`(400 拒空/以 / 结尾的 key)+ `GET /admin/knowledge/indexed`。独立自检 45 断言全绿(全 mock,不触网、不进 tests/)。
  - **4b-2 前端浏览 + 数据层** ✅ DONE(2026-09-19)—— `api.ts` 追加知识库全套类型 + `ApiError`(带 HTTP 状态码,区分 503「未接通」与其它失败)+ `detailOr`/`kfetchJson`/`kfetchVoid` 帮手 + 8 个函数(getTree/createFolder/uploadFiles/deleteFile/deleteFolder/indexFile/indexedKeys/reindexKnowledge);新增 `composables/useKnowledge.ts`(**模块级单例**:prefix/tree/loading/error/storageEnabled/indexReady/indexedSet + loadTree/enter/up/goto/refresh/isIndexed;loadTree 先取树,503→storageEnabled=false 走空态,再**单独**取 indexedKeys,Qdrant 未接通失败则静默降级 indexReady=false 不显徽标——避免把「未索引」和「查不到」混为一谈);新增 `KnowledgeBreadcrumb.vue`(前缀拆逐级面包屑,末级 is-current 禁用)+ `KnowledgeView.vue`(只读浏览:面包屑 + 刷新 + 子分类行可进入 + 文件行含 已索引/未索引 徽标 + size/日期;存储未接通/错误/空节点三态卡片;indexReady=false 时不显徽标只留提示);`AppHeader.vue` 加「对话 | 知识库」分段切换(props `view` + emit `change-view`,☰ 菜单仅对话视图显示);`App.vue` 加 `view` ref,知识库视图下用 `<template v-if>` 隐藏历史侧栏/抽屉并渲染 `<KnowledgeView v-else>`。`npm run build` 通过(仅既有 >500KB chunk 告警,无类型错)。**本小步纯只读,不含增删改/上传。**
  - **4b-3 前端增删改 + 自动索引状态 + 维护重建** ✅ DONE(2026-09-19)—— `useKnowledge.ts` 加写操作:`makeFolder(name)`(建分类后刷新)、`uploadAndIndex(files)`(先 `uploadFiles` 整批传 OSS,回来按逐文件结果建进度行 uploaded→indexing;刷新树让新文件即现;再**串行**逐个 `indexFile`,徽标即时点亮;**遇 503 停手把余下标失败**免连打必失败请求)、`removeFile`/`removeFolder`(删后刷新)、`runReindex(scope)`(维护兜底);状态 `uploadRows`/`uploading`/`reindexing`/`reindexResult`。新组件 `KnowledgeToolbar.vue`(「新建分类」行内表单含名字合法性前拦 + 「上传文件」隐藏 multiple input + 上传进度列表按 phase 显文案/语气 ok青·bad红·muted灰·pending)、`ReindexBar.vue`(可折叠维护区,**只提供「全量重建整库」+ 二次确认**——见下「重建陷阱」)。`KnowledgeView.vue` 重构:三态卡片保留,已接通分支挂 Toolbar + 内容面板 + ReindexBar,文件行/子分类行加行内删除确认(复用 HistorySidebar 的 ✕→删/取消 模式,删除失败就地红条不打爆整面板)。`npm run build` 通过(276 模块,仅老样子 >500KB chunk 警告)。
    - ✅ **重建陷阱已解(2026-10-03):** 安全发布已落地 —— 任务写入**候选版本集合**(`{QDRANT_COLLECTION}__<UTC 时间>-<随机>`,范围外向量原样复制、范围内整份重建),校验向量数与源文件未变后翻转 `index-manifest.json` 指针;**任何目标文件失败就不发布,旧索引继续可检索**。前端因此已恢复「当前分类」的非破坏重建(scope `kind=prefix`),原 `recreate_collection()` 清库行为与 `delete_by_prefix` 待办一并作废。
    - 🔁 **索引入口收敛(2026-10-03):** 上述 `index_file` / `reindex` 同步入口连同 `POST /admin/knowledge/index`、`POST /admin/knowledge/reindex` 已删除(生产未上线,不留兼容层);唯一入口是 `POST /admin/knowledge/index-jobs`(单文件 = `scope.kind=keys` 单元素任务)。前端 `indexFile` / `reindexKnowledge` 同步移除:上传后自动索引、行内重试 / 批量重试都改为建 keys 任务,上传行在**任务结束时按最终发布结果结算**(发布失败不会误报"已索引")。详见 [docs/OCR接入技术方案.md](docs/OCR接入技术方案.md) §15。
- **后置优化** —— ① 索引增量化(记 hash/mtime 跳过未变文件),减少全量重建开销(OCR 命中缓存只省识别费,切块与 Embedding 仍会整份重跑)。② ~~按前缀非破坏重建~~ **✅ 已解决(2026-10-03)**:候选版本 + 发布指针,前端已有「整库 / 当前分类」两种范围(见上)。

---

## SOP 上 OSS + 前端 CRUD —— ✅ DONE(2026-09-20,待用户功能性验证)

> **目标**:把 SOP(标准作业流程)从本地 `backend/sops/*.md` 迁到阿里云 OSS(与知识库**同桶、不同前缀** `sops/`),后端提供查看 + 编辑的 CRUD 接口,前端给用户看 SOP 并做增删改查。

### 已确认的设计决定(2026-09-20)

- **OSS 前缀参数化**:把 OSS 增删改查抽成共享工具,SOP 与知识库都调它,**前缀作为独立参数注入**。落地 = `OssStore(prefix)` 类 + `knowledge_store()` / `sops_store()` 两个单例工厂。
- **SOP 表示**:按 SOP 文档真实结构做**结构化表单**(id / name / description / triggers / 正文 body),前端**不用原始表单样式**,做美化后的表单 UI。
- **无本地兜底**:不保留本地目录读取,完全重构为 OSS 读。
- **废弃字段 `sops_dir` 直接删除**(第 2 步随 loader 重写一并删)。
- **不迁移现有 `backend/sops/*.md`**:那是测试文档,不迁。
- **前端布局:全屏三态切换版**(列表 → 详情 → 编辑),即 `frontend/mockups/sop-view.html` 原型;非左右分栏(`sop-view-split.html` 为已否决备选,保留作参考)。

### 步骤与进度(一步一步;不用工作流)

- **1 后端 OSS util 重构** ✅ DONE(2026-09-20)—— [oss.py](backend/app/rag/oss.py) 从单一硬编码根前缀重写为 `OssStore(prefix)` 类(前缀作独立参数,`_full`/`_rel` 及全部增删改查/签名/连通性自检收为实例方法;`get_bucket()`、`_encode_meta`、`_META_PREFIX` 仍模块级,桶与前缀无关全局共用)+ `knowledge_store()`(`oss_prefix`,默 `knowledge/`)/ `sops_store()`(`oss_sops_prefix`,默 `sops/`)两个 `@lru_cache` 工厂。构造只记归一化前缀、不连 OSS(未配也能实例化,只有调方法命中 `get_bucket()` 才抛 → 优雅降级契约不变)。[config.py](backend/app/config.py) 加 `oss_sops_prefix="sops/"`(`sops_dir` 留第 2 步删)。11 处调用点(knowledge.py×7 / ingest.py×3 / chat.py×1)全改为 `oss.knowledge_store().X()`,知识库行为与重构前**逐字不变**(同前缀、同逻辑)。验证:5 文件 py_compile 通过;`qa-agent` 环境 `pytest` 23 passed;工厂前缀隔离与 `_full` 拼接自检正确。
- **2 config + loader 重写** ✅ DONE(2026-09-20)—— [config.py](backend/app/config.py) 删 `sops_dir` 字段;[loader.py](backend/app/skills/loader.py) 从「本地 `sops/*.md` 目录」改为读 `oss.sops_store()`(`list_all()` 拿 `.md` 清单 → `get_object` → `frontmatter.loads`),`lru_cache` 与 `get_catalog/get_skill/reload` **签名逐字不变**(search.py/tools.py/trace.py/chat.py 零改)。**优雅降级**:OSS 未配(`get_bucket` RuntimeError)/ 不可达 → `_load()` 兜 `except Exception` 返回空目录;单篇坏文件跳过并告警不影响整表。文档同步:[.env.example](backend/.env.example) 删 `SOPS_DIR`、在 OSS 段加 `OSS_SOPS_PREFIX=sops/`;[README.md](backend/README.md) 目录结构去掉本地 `sops/`、loader 描述改「从 OSS sops/ 前缀读」。测试改造:SOP 已迁 OSS,不能再靠本地临时目录注入 → 新增 [tests/conftest.py](backend/tests/conftest.py)(`FakeSopsStore` 内存假仓库 + `install_sops` 夹具接管 `app.rag.oss.sops_store`),[test_skills](backend/tests/test_skills.py)/[test_search](backend/tests/test_search.py)/[test_trace](backend/tests/test_trace.py)/[test_stream](backend/tests/test_stream.py) 的 `SOPS_DIR`+tmp_path 注入全改为 `install_sops({...})`。验证:py_compile 通过;`qa-agent` 环境 `pytest` **23 passed**;降级路径实测 `reload()`→0、catalog 空、不抛异常。
- **3 迁移** —— 跳过(测试文档,不迁)。
- **4 后端 SOP CRUD 路由** ✅ DONE(2026-09-20)—— [skill.py](backend/app/schemas/skill.py) 加 `SopSummary`(id/name/description/triggers/key/updated_at)/`SopDetail`(+body)/`SopWrite`(id/name/description/triggers/body)。[oss.py](backend/app/rag/oss.py) `OssStore` 补 `stat(key)`(head 取 size+最后修改 Unix 秒,不存在返回 None,供详情时间戳/存在性判断)。新增 [sops.py](backend/app/api/routes/sops.py) 挂 `/api/v1/sops`:`GET`(列表,不含正文,按 id 升序,单篇坏文件跳过告警)、`GET /{id}`(详情,404)、`POST`(201,重名 409,非法 id 400)、`PUT /{id}`(404,路径 id≠内容 id→400)、`DELETE /{id}`(204,404);每次写后 `loader.reload()` 让 LLM 侧目录即时同步。约定:每篇 = OSS `sops/<id>.md`(YAML frontmatter + 正文),id 即文件名、**不支持改名**(要改删旧建新);id 白名单 `^[A-Za-z0-9][A-Za-z0-9_-]*$` 挡路径穿越;`frontmatter.dumps/loads` 序列化;同步 `def` 走线程池;OSS 未配(`get_bucket` RuntimeError)→ 503。[main.py](backend/app/main.py) 挂 `sops.router`。测试:[conftest.py](backend/tests/conftest.py) 的 `FakeSopsStore` 扩到 object_exists/put_object/delete_object/stat + `prefix`;新增 [test_sops.py](backend/tests/test_sops.py) 13 项(列表/详情/新建/更新/删除全路径 + 409/400/404 + 写后 loader 即时同步)。验证:py_compile 通过;`qa-agent` 环境 `pytest` **36 passed**。
- **5 前端 SopView** ✅ DONE(2026-09-20)—— [api.ts](frontend/src/api.ts) 加 `SopSummary`/`SopDetail`/`SopWrite` 类型 + `listSops`/`getSop`/`createSop`/`updateSop`/`deleteSop`(复用 `kfetchJson`/`kfetchVoid`,503→`ApiError`)。新增 [useSops.ts](frontend/src/composables/useSops.ts) 模块级单例:`list`/`loading`/`error`/`storageEnabled`(503→false 走空态)/`loaded`/`mode`(list|detail|editor)/`current`/`editingNew`/`detailLoading`/`detailError`/`saving`/`saveError` + `loadList`/`showList`/`openDetail`/`openEditor(id?)`/`save`(create/update→刷新列表→跳详情)/`remove`。新增 [styles/sop.css](frontend/src/styles/sop.css)(取自已批准原型,SOP 专属类;不重定义全局 `.md`/页头/令牌;内容栏 960px 写死)+ [SopView.vue](frontend/src/components/SopView.vue)(编排:按 mode 切三子视图,onMounted 首屏载一次)+ [SopList.vue](frontend/src/components/SopList.vue)(卡片列表 + 行内删除确认 + 存储未接通/失败/空态)+ [SopDetail.vue](frontend/src/components/SopDetail.vue)(`.doc` 只读 + `MarkdownView` 正文 + 「更新于 X · 存于 sops/id.md」)+ [SopEditor.vue](frontend/src/components/SopEditor.vue)(id/name/desc 字段 + triggers 芯片输入回车加/Backspace删 + 插入标准章节 + 编辑/预览切换 + 就地保存/删除确认;id 客户端预校验 `^[A-Za-z0-9][A-Za-z0-9_-]*$`,编辑既有篇时 id 禁改)。[AppHeader.vue](frontend/src/components/AppHeader.vue) 加第三折页标签「SOP 流程」;[App.vue](frontend/src/App.vue) View 加 `sops`、`viewFromHash` 认 `#/sops`、`viewComponent` computed 三视图映射进 KeepAlive。验证:`npm run build`(vue-tsc)**通过**,无类型错误。待用户功能性验证。

---

## 前端界面打磨

- **标签卡导航 + 切换保活 + 空态居中** ✅ DONE(2026-09-20)—— 「对话 / 知识库」从分段胶囊(`.hd__seg`)改为**折页标签(方案 A)**:标签在页头右侧,活动标签白底(=内容区 `--paper` 同色)、去下边框、`margin-bottom:-1px` 压在页头底线上 + 顶部 `inset 0 2px var(--primary)` 科研青,与内容连成一体(页头改 `align-items:flex-end`,lead/theme 靠 padding/margin 抬离底线视觉齐平)。
  - **切换保活**:对话主区抽成 [ChatView.vue](frontend/src/components/ChatView.vue);[App.vue](frontend/src/App.vue) 用 `<Transition name="view-fade" mode="out-in">` + `<KeepAlive>` 包 `<component :is>`,两视图状态 / DOM 常驻——对话滚动位置经 `onDeactivated/onActivated` 存还原(重挂 DOM 可能把 scrollTop 归零)、输入框草稿、知识库当前分类来回切换都不丢。
  - **hash 路由**:`#/chat` / `#/knowledge`——`viewFromHash` 读取、`changeView` 写 hash(入历史,前进/后退可用)、`hashchange` 反向同步、首屏 `replaceState` 规整不增历史条目;刷新后停在当前视图、链接可分享。
  - **淡入过渡**尊重 `prefers-reduced-motion`。
  - **空态居中(点 2 修复)**:[EmptyState.vue](frontend/src/components/EmptyState.vue) 去掉固定 `52px` 顶距;`ChatView` 无消息时 `.chat__thread--empty` 用 flex 把邀请块垂直 + 水平居中到空画布中心。
  - `npm run build` 通过(仅既有 >500KB chunk 警告)。
- **两视图布局统一 · 消除切换横移(方案乙)** ✅ DONE(2026-09-20)—— 起因:对话页内容在「窗口−260px 侧栏」里居中,知识库在整窗里居中,切换时页头 + 内容一起横移,很不和谐。改法:历史侧栏由桌面常驻左栏改为**所有宽度都是固定滑出抽屉**(`position:fixed` + `translateX(-100%)`,`.app--drawer` 滑入 + 半透明遮罩),不再占布局 → `app__main` 两视图都整窗 → 内容栏(`.thread` / `.kv__wrap`,均 `max-width:860 margin:auto`)在两视图都整窗居中,**切换零横移**。
  - [App.vue](frontend/src/App.vue):侧栏 + 遮罩不再按 `view==='chat'` 条件渲染,常挂两视图;抽屉开关 `.app--drawer` 只看 `sidebarOpen`;删掉原 `@media(max-width:860px)` 的窄屏专属抽屉规则(现为默认);新增 `onSideNavigate`——从抽屉「新对话 / 打开某通」时收起抽屉并 `changeView('chat')` 切回对话。
  - [AppHeader.vue](frontend/src/components/AppHeader.vue):☰ 去掉 `v-if="view==='chat'"`,`.hd__menu` 默认 `display:grid`,删掉窄屏 media query → ☰ 在**两视图 + 桌面**都在,页头两视图完全一致(logo 不横移);历史抽屉全局可开,知识库里点开选会话会自动切回对话。
  - 代价(已与用户确认):历史不再桌面常驻,需点 ☰ 打开。`npm run build` 通过(仅既有 >500KB chunk 警告)。
- **会话工具胶囊 + 搜索框(前端)** ✅ DONE(2026-09-20)—— 把开合边栏的 ☰ 从页头挪到「页头下方、与内容栏左对齐」的分段胶囊(仿之前的 switch 胶囊),三键:**边栏**(开合抽屉)/ **搜索**(开抽屉并聚焦搜索框)/ **新对话**。胶囊**仅对话视图**出现(三动作皆会话相关;知识库无 → 两视图页头彻底一致、零横移)。
  - 新增 [useSidebar.ts](frontend/src/composables/useSidebar.ts) 模块级单例:`open` + `focusSearchSignal` + `toggleDrawer/openSearch/closeDrawer`,把抽屉开关与搜索聚焦在 App / 胶囊 / 抽屉三处共享(避开动态 `<component>` 的 prop 透传)。
  - [AppHeader.vue](frontend/src/components/AppHeader.vue) 去掉 ☰ 及其 `historyOpen` prop / `toggle-history` emit / `.hd__menu` 样式,页头只剩 字标 + 标签 + 主题。[App.vue](frontend/src/App.vue) 改用 `useSidebar` 的 `open`/`closeDrawer`。[ChatView.vue](frontend/src/components/ChatView.vue) 顶部加 `.tools` 胶囊(内联 SVG 图标 + 文字,分段分隔线),`.chat__thread` 顶距相应调小。
  - **搜索:仅前端显示**(已与用户确认)—— [HistorySidebar.vue](frontend/src/components/HistorySidebar.vue) 抽屉顶部加搜索框(`type=search`,本地 `searchText` 未接过滤),点胶囊「搜索」经 `focusSearchSignal` 聚焦。**TODO(后端):真正按对话内容检索**。`npm run build` 通过。

---

## 记忆系统

### Agent 跨轮对话记忆(Redis checkpointer)—— 后端 DONE(2026-09-18)

- 持久化选 **Redis**(非 Postgres):对话是 JSON、ECS 已装 Redis、LangGraph 有 Redis Saver。
- 用 `langgraph-checkpoint-redis`(0.3.9)的 **`AsyncRedisSaver`**,在 `main.py` 的 FastAPI `lifespan` 里挂进图 → `app.state.graph`;`thread_id` 作会话钥匙,`/chat` 回传、`/chat/stream` 先发 `meta{thread_id}`。
- **优雅降级**:没配 `REDIS_URL` 或 Redis 初始化失败 → 打告警、退回单轮模式,后端照常起。
- **✅ 前置已就绪(2026-09-18)**:ECS 原生 Redis 已从 6.0.16 升级到 **8.10.2**(官方 apt 源),`loadmodule` 加载了 `rejson.so` + `redisearch.so`,`MODULE LIST` 可见 `search` 与 `ReJSON`;`bind 0.0.0.0` + `requirepass` + 安全组放行开发机;后端日志确认 `Redis checkpointer 已启用`。
  - 升级踩坑记录:① `daemonize/supervised` 与 systemd `Type=notify` 不匹配 → 启动 `Result: protocol`,改 `daemonize no` + `supervised systemd`;② 保留旧配置导致模块没加载(只有编译进内核的 `vectorset`)→ 手动加 `loadmodule`;③ 远程连不上是 `bind` 仍为回环(错误码 10061 refused,非超时)→ 改 `bind 0.0.0.0`。
  - 真 `.env` 已填 `REDIS_URL=redis://:密码@8.135.60.136:6379/0`(redis-py 裸 TCP,不需要 NO_PROXY)。进生产前轮换一次 requirepass。

### 历史对话列表 —— DONE(2026-09-19)

- **以后端 Redis 为准**(非 localStorage),单用户、无鉴权:扫描 checkpointer 索引里的全部线程做列表展示;删除连带清 Redis 记忆。
- 用 `AsyncRedisSaver` 的现成异步 API(已核对 0.3.9 源码):列全部 `alist(None)`(config=None → 过滤器 `*`,按 checkpoint_id ULID 倒序);读一通 `aget_tuple({"configurable":{"thread_id":tid}})`(走 `checkpoint_latest` 指针取最新态);删一通 `adelete_thread(tid)`(清该线程全部 checkpoint + writes + 指针)。消息取自 `checkpoint["channel_values"]["messages"]`,时间取 `checkpoint["ts"]`(ISO)。
- 后端:`main.py` 把 checkpointer 也挂到 `app.state`(原只挂 graph);新增 `app/api/routes/conversations.py`(独立路由,和 health/chat 并列)+ `app/schemas/conversation.py`。三端点 `GET /conversations`(返回 `{enabled, items}`,Redis 没开 → enabled=false 空列表)、`GET /conversations/{tid}`(回放 Q&A)、`DELETE /conversations/{tid}`(204;Redis 没开 → 503)。
- 前端:`api.ts` 加 3 函数 + 类型;`useChat.ts` 改**模块级单例**(App 与侧栏共享态)+ 加 `conversations/historyEnabled/loadConversations/openConversation/removeConversation`,首轮拿到 thread_id 后自动刷新列表;新增 `HistorySidebar.vue`(列表 + 新对话 + 行内删除确认 + 活跃高亮 + 空态/未启用态);`App.vue` 改两栏(窄屏侧栏变滑出抽屉);`AppHeader.vue` 移除「新对话」(挪进侧栏),改为窄屏可见的抽屉开关。
- **回放精度**:只还原最终答案文本(每条助手答案塞进单一 step),不重建当时的工具调用时间线(Redis 存的是最终消息;实时对话仍有完整时间线)。
- 已验:后端 `create_app()` OpenAPI 三端点就位、`_replay/_title` 对真实 LangChain 消息处理正确;前端 `npm run build` 通过。
- 小注:RediSearch 索引近实时,首轮刚建完偶有毫秒级延迟才被 `alist` 扫到;`alist` 默认扫上限 10000 条 checkpoint(单用户够,超了再加分页)。

### 待办

- **端到端冒烟(你来点)**:① 同一通对话连问两轮(先"我是安心,请记住名字",再"我是谁") → 第二轮应答出"安心";② 历史栏应列出该对话,点开能回放,删除后从列表消失且再问不再记得。
- **✅ 前端(2026-09-18 DONE)**:`api.ts` 发送带 `thread_id`、接住后端 `meta` 事件;`useChat.ts` 存 `threadId`(内存态,刷新即新开一通)+ 每次回传 + `newConversation()`;头部加「新对话」按钮。**注:** 暂不落 localStorage —— 消息列表没持久化,只存 thread_id 会造成"刷新后界面空白但模型还记得"的错位;要持久化需连消息一起存,留作后续。
- **下一步:长对话摘要压缩**(消息累积到阈值 → 摘要压缩早期轮次,控制 token / 上下文长度)。

---

## 已完成(参考)

- RAG Step 1 地基:`config.py` / `.env.example` / `app/rag/{embeddings,store}.py`,连通性已端到端验证。
- 环境坑均在启动层解决(`start-dev.sh`):`SSL_CERT_FILE` 指 certifi;VPN 代理导致 502 → 动态 `NO_PROXY`。
