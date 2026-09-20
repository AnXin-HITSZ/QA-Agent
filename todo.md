# QA-Agent 待办 & 设计固化

> 本文件记录已确认的设计与"动手前待确认项",便于取到文件后接着落实。
> 约定:一步一步来,不使用工作流。

---

## RAG 知识库(Step 2)—— 进行中

> **架构**:原件存**阿里云 OSS**(私有桶,唯一事实源,抗 ECS 重建);ECS 的 **Qdrant** 只放向量 + 元数据;引用来源用短时效**签名 URL** 指回原件。
> **分类 = 用户在前端手建的 OSS 前缀节点**(人工放置,不再按关键词猜);用户**只能上传散文件**到某节点(平铺 `前缀+文件名`),不能上传目录树。

### 已确认的关键决定

- **Embeddings**:`text-embedding-v4`(默认 1024 维,OpenAI 兼容端点,换模型才改 `EMBEDDINGS_DIM`)。限制:单请求 ≤ 10 条输入、单条 ≤ 8192 token → 摄取分批。
- **涉密红线**:已确认切块原文可发 DashScope 云 + 向量存云 ECS Qdrant + 扫描件 OCR 正文也可上云 → 维持云方案,不做本地 embedding / 本地 Qdrant。
- **原件仓库**:阿里云 OSS 私有桶 `anxin-hitsz-qa-agent`(region cn-shenzhen,prefix `knowledge/`)+ SSE 服务端加密 + RAM 最小权限;AccessKey 只进 `backend/.env`,**绝不提交**。
- **发票 / 报销**:文件名已编码金额(`发票6100`)、目录名已编码费用大类+实际支出+预算上限 → 报销/审计问答可直接解析文件名算账;`metadata.py` 富化字段与分类**正交、非权威**。
- **抽取器分派**:按文件扩展名(见 2c);PDF 运行时再判电子版 / 扫描件。

### 步骤与进度(一步一步;不写测试进生产代码,只做独立自检)

- **2a OSS 接入层** ✅ DONE(2026-09-19)—— `app/rag/oss.py`(oss2 薄封装:put/get/list/sign/delete + 目录标记 + `x-oss-meta-*` 下划线↔连字符对称转换)。真桶读写往返 + 私有桶签名下载自检全绿。
- **2b 知识库树端点** ✅ DONE(2026-09-19)—— `app/api/routes/knowledge.py` + `app/schemas/knowledge.py`,挂 `/api/v1/knowledge`。5 操作:`GET /tree`、`POST /folder`、`DELETE /folder`(空 prefix→400 防清库)、`POST /upload`(多文件平铺,重名 `skipped_exists` 不覆盖,逐文件返回结果)、`DELETE /object`。处理器全用**同步 `def`**(FastAPI 丢线程池,oss2 阻塞调用不堵流式对话)。真桶 12 断言 HTTP 自检全绿。
- **2c 抽取器 + 富化** ✅ DONE(2026-09-19)—— `app/rag/extract.py`(按扩展名抽文本:pdf/docx/xlsx/pptx/txt/xls;扫描件与图片标 `needs_ocr`;坏文件不抛异常、只标 `error`,整批不中断)+ `app/rag/metadata.py`(纯正则从路径+文件名解析 project/person/fee_category/spent/budget/invoice_amount/doc_type)。独立自检全绿。
- **2d OCR** ⏸ **接口已预留、引擎延后接**(2026-09-19)—— `app/rag/ocr.py`:`ocr_image(bytes) -> str` + `ocr_available()`,按 `.env` 的 `OCR_ENGINE` 分发(`aliyun` / `rapidocr` 骨架)。**目前 `_IMPLEMENTED` 为空 → OCR 恒为关闭**:未接入 / 未配 → 抛 `OCRNotConfigured`,由 2e 摄取捕获后**跳过该文件并告警**(优雅降级,同 Redis/OSS 缺失风格)。**大概率选阿里云 OCR**;接入 = 补 `_ocr_aliyun` 函数体 + 把 `"aliyun"` 加进 `_IMPLEMENTED` + `.env` 设 `OCR_ENGINE=aliyun`,**调用方(extract / 2e)零改**。有空再回头做。
- **2e 摄取管线 + reindex** ✅ DONE(2026-09-19)—— `app/rag/ingest.py`:`reindex(prefix="")` 遍历 OSS(`list_all`)→ 逐文件 `get_object` 下载 → `extract` 抽文本 →(`needs_ocr`:OCR 未接入则跳过告警;接入后仅图片直接 OCR,扫描 PDF 待 2d 渲染)→ `_pack` 切块(目标 1000 字/块、超长块带 150 重叠硬切)→ `parse_metadata` 富化 payload(`text/oss_key/source/category=手建前缀/chunk_index/ext` + project/person/fee_category/...)→ 分批 embedding(`chunk_size=10`,≤10/请求)→ `store.upsert`(每批 128)。point id = `uuid5(NS,"<key>#<i>")` 确定性(便于将来增量)。单文件异常/坏文件优雅降级(跳过记原因、不中断整批)。`store.py` 加 `recreate_collection()`+`make_point()`;`embeddings.py` 加 `chunk_size=10`。端点 `POST /api/v1/admin/knowledge/reindex`(`admin_router`,同步 `def` 走线程池;先验依赖再清库,OSS/Qdrant/Embeddings 缺 → 503)。v1 = 清空 + 全量重建;增量(hash/mtime)后置。独立自检 30 断言全绿(纯逻辑 + 全 mock 端到端,不触网、不进 tests/)。
- **3 检索工具 + 引用来源** ✅ DONE(2026-09-19)—— `app/rag/retrieve.py`:`search_knowledge(query, top_k=5, score_threshold=0.2)`(**v1 纯语义、全库、不带过滤** —— `store.search` 一行未改)embed_query → 检索 → 丢弃低于阈值 / 空正文 → 结构化命中(text/oss_key/source/category/score/chunk_index/ext)+ `format_hits` 拼给 LLM 的编号片段。`tools.py` 加 `@tool(response_format="content_and_artifact") search_knowledge`:返回 `(给 LLM 的文本, hits artifact)`,未配置 / 零命中优雅降级,进 `TOOLS`(agent 自主决定何时调,**不加预检索节点**、图结构不变 `START→agent⇄tools→END`)。**(2026-09-20 加固)** 降级 `except` 由只兜 `RuntimeError` 放宽到 `except Exception`——原先兜不住 embedding/向量供应商的 HTTP 错误(如 DashScope 欠费 `openai.BadRequestError 400 Arrearage`),会穿透 ToolNode 把整条 SSE 流打崩 500;现在任何检索失败都降级为「知识库暂时无法访问、据常识回答」,详细异常写 `logger.warning` 便于排查,只回 LLM 一句干净提示(不把冗长报错喂给模型)。`trace.py` 加 `used_sources(messages, top_n=5)`:读 `ToolMessage.artifact`、**限定最后一个 HumanMessage 之后(本轮)**、按 `oss_key` 去重(留最高分)、按分降序 —— 避开 checkpointer 全量历史里的旧来源。`nodes.py` SYSTEM_PROMPT 微调一句(日常答疑先查库、查不到再据常识并说明)。`schemas/chat.py` 加 `SourceCitation`(oss_key/source/category/score/url)+ `ChatResponse.sources`(默认空、非破坏)。`chat.py` 加 `_sign_sources`(**emit 时**用 `oss.sign_url` 现签 15min URL、不存库;签名失败 url 留空仍返回元信息);`/chat` 回填 `sources`,`/chat/stream` 把 tools 节点消息并入 `seen` + `done{skill, sources}`。独立自检(全 mock embeddings/store/sign_url,不触网、不进 tests/)全绿。**score_threshold=0.2 为经验默认,需按真实语料再调。**
- **4a 参考来源展示** ✅ DONE(2026-09-19)—— 消费 `ChatResponse.sources` / SSE `done` 事件的 `sources`,答案末尾**常驻列表**(不折叠)。`api.ts` 加 `Source` 接口 + `ChatResponse.sources` + `StreamHandlers.onDone(skill, sources)` + `done` 分派解析 sources;`useChat.ts` 的 `Msg.sources`、`reply` 初始 `sources:[]`、`onDone` 回填 `reply.sources`;`AnswerRecord.vue` 加 `sources` prop + `srcName`(优先文件名、退取 oss_key 末段)+ `<footer class="ar__sources">` 常驻清单(`s.url` 可点 `target=_blank rel=noopener`,url 为 null 显示「链接不可用」不可点;类别灰字 + 极淡分数)+ 发丝线分隔的 scoped CSS;`App.vue` 传 `:sources="m.sources"`。历史回放(openConversation)不带 sources → 回放答案不显来源(一致)。`npm run build` 通过。
- **4b 前端知识库管理视图 + 增量索引**(拆 3 小步;导航 = 面包屑 + 单层列表)
  - **4b-1 后端增量索引** ✅ DONE(2026-09-19)—— 新需求:**上传即建索引、删文件即删索引**(不再只靠全量重建)。`store.py` 加 `delete_by_oss_key(key)`(按 payload `oss_key` 精确匹配删点,collection 不存在→0,best-effort)+ `indexed_keys(prefix)`(按 `category==prefix` 分页 scroll 拿已索引 key 集合,供徽标);`ingest.py` 抽出共享 `_file_points(key,res,text)`(切块+payload+确定性 id,reindex 与增量共用),加 `index_file(key)`(先验 embeddings → 先删旧点(幂等)→ 抽取/切块/向量化/upsert;needs_ocr/unsupported/空→indexed=False+reason 仍返回;运行时错不外抛只记 reason;Embeddings/Qdrant 未配→抛 RuntimeError 交路由转 503)+ `delete_file_index(key)`;`schemas/knowledge.py` 加 `IndexFileRequest/IndexFileResult/IndexedKeysResult`;`knowledge.py` 加 logger、`_oss_call`→`_dep_503`(广涵 OSS/Qdrant/Embeddings)、`_drop_vectors`(删原件/节点后 best-effort 连带清向量,失败只告警不阻断),`delete_file`/`delete_folder`(删前先列 key 再逐个清向量)接线,新增 `POST /admin/knowledge/index`(400 拒空/以 / 结尾的 key)+ `GET /admin/knowledge/indexed`。独立自检 45 断言全绿(全 mock,不触网、不进 tests/)。
  - **4b-2 前端浏览 + 数据层** ✅ DONE(2026-09-19)—— `api.ts` 追加知识库全套类型 + `ApiError`(带 HTTP 状态码,区分 503「未接通」与其它失败)+ `detailOr`/`kfetchJson`/`kfetchVoid` 帮手 + 8 个函数(getTree/createFolder/uploadFiles/deleteFile/deleteFolder/indexFile/indexedKeys/reindexKnowledge);新增 `composables/useKnowledge.ts`(**模块级单例**:prefix/tree/loading/error/storageEnabled/indexReady/indexedSet + loadTree/enter/up/goto/refresh/isIndexed;loadTree 先取树,503→storageEnabled=false 走空态,再**单独**取 indexedKeys,Qdrant 未接通失败则静默降级 indexReady=false 不显徽标——避免把「未索引」和「查不到」混为一谈);新增 `KnowledgeBreadcrumb.vue`(前缀拆逐级面包屑,末级 is-current 禁用)+ `KnowledgeView.vue`(只读浏览:面包屑 + 刷新 + 子分类行可进入 + 文件行含 已索引/未索引 徽标 + size/日期;存储未接通/错误/空节点三态卡片;indexReady=false 时不显徽标只留提示);`AppHeader.vue` 加「对话 | 知识库」分段切换(props `view` + emit `change-view`,☰ 菜单仅对话视图显示);`App.vue` 加 `view` ref,知识库视图下用 `<template v-if>` 隐藏历史侧栏/抽屉并渲染 `<KnowledgeView v-else>`。`npm run build` 通过(仅既有 >500KB chunk 告警,无类型错)。**本小步纯只读,不含增删改/上传。**
  - **4b-3 前端增删改 + 自动索引状态 + 维护重建** ✅ DONE(2026-09-19)—— `useKnowledge.ts` 加写操作:`makeFolder(name)`(建分类后刷新)、`uploadAndIndex(files)`(先 `uploadFiles` 整批传 OSS,回来按逐文件结果建进度行 uploaded→indexing;刷新树让新文件即现;再**串行**逐个 `indexFile`,徽标即时点亮;**遇 503 停手把余下标失败**免连打必失败请求)、`removeFile`/`removeFolder`(删后刷新)、`runReindex(scope)`(维护兜底);状态 `uploadRows`/`uploading`/`reindexing`/`reindexResult`。新组件 `KnowledgeToolbar.vue`(「新建分类」行内表单含名字合法性前拦 + 「上传文件」隐藏 multiple input + 上传进度列表按 phase 显文案/语气 ok青·bad红·muted灰·pending)、`ReindexBar.vue`(可折叠维护区,**只提供「全量重建整库」+ 二次确认**——见下「重建陷阱」)。`KnowledgeView.vue` 重构:三态卡片保留,已接通分支挂 Toolbar + 内容面板 + ReindexBar,文件行/子分类行加行内删除确认(复用 HistorySidebar 的 ✕→删/取消 模式,删除失败就地红条不打爆整面板)。`npm run build` 通过(276 模块,仅老样子 >500KB chunk 警告)。
    - ⚠️ **重建陷阱(已避坑,记后端待办):** [ingest.py](backend/app/rag/ingest.py) 的 `reindex(prefix)` **无论带不带前缀都先 `recreate_collection()` 清空整个 collection** 再只重建该前缀 → 带前缀重建会**误删其它节点索引**。故前端**只暴露「整库重建」**,不做「重建当前节点」。要支持按节点非破坏重建,需后端改为「先按前缀 `delete`(如新增 `delete_by_prefix`)再重嵌该前缀」——留作后端后置。
- **后置优化** —— ① reindex 增量化(记 hash/mtime 跳过未变文件),减少全量重建开销。② **按前缀非破坏重建**(见上「重建陷阱」),之后前端可恢复「重建当前节点」选项。

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
