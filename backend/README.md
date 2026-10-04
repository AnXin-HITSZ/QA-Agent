# Lab QA Assistant — 后端

实验室问答 + 财务报销 SOP 引导 Agent 的后端。核心机制:**手搭 ReAct agent —— 模型
自行调用工具列出 OSS 中的 SOP 元信息目录、选择并读取正文,再依据正文生成分步指引**;日常答疑类问题
直接回答。

- Skill 就是 OSS `sops/` 前缀下的一个 Markdown(frontmatter 记元信息 + 正文写 SOP),
  **新增一个流程只需丢一个 md 文件,无需改代码**。
- LLM 不手写适配层,由 `.env` 驱动、用 LangChain 的 `ChatOpenAI` 创建(OpenAI 兼容端点)。
  选哪篇 SOP 交给模型的 tool-calling 推理,不再由代码里硬编码的意图识别或选择节点决定。

## 目录结构

```
backend/
  app/
    main.py               FastAPI 入口(CORS + 路由)
    config.py             配置(读取 .env,含 LLM 连接参数、OSS 前缀)
    llm.py                get_llm():从 .env 建 ChatOpenAI(需真实 LLM_API_KEY)
    skills/
      loader.py           从 OSS sops/ 前缀读 *.md → get_catalog() / get_skill(id) / reload()
    graph/
      state.py            ChatState(messages:add_messages 累积 ReAct 轨迹)
      tools.py            @tool list_sops / get_sop + TOOLS(经 bind_tools 注入)
      nodes.py            agent 节点(LLM 绑定工具 + 行为护栏 SYSTEM_PROMPT)
      builder.py          START → agent ⇄ tools → END(tools_condition 判定)
      trace.py            used_sop_id(messages):从轨迹回读本次引用的 SOP
    api/routes/
      health.py           GET /health
      chat.py             POST /chat、/chat/stream(SSE)、/admin/sops/reload
    schemas/
      chat.py             ChatRequest / ChatResponse
      skill.py            SkillMeta(目录项)
  tests/
    test_health.py · test_graph.py · test_skills.py
    test_catalog.py · test_trace.py · test_stream.py
  requirements.txt · .env.example · pytest.ini
```

## 对话图(ReAct)

```
START → agent ──(无 tool_calls)────────────→ END
          ⇅
        tools   (agent 发起 tool_calls 时执行,执行完回到 agent)
```

- `agent`:`get_llm().bind_tools(TOOLS)` 推理。要么直接给出最终回答,要么发起对工具的
  调用请求。工具经模型原生 tool-calling 通道注入,**不写进 prompt**;`SYSTEM_PROMPT`
  只立行为护栏(何时检索、严格依据 SOP、不编造、不接报销系统),不点名具体工具。
- `tools`:`ToolNode` 执行 agent 请求的工具,把结果作为 `ToolMessage` 追加回状态,再交回
  `agent`。`tools_condition` 依据最后一条消息是否带 `tool_calls` 决定去 `tools` 还是 `END`。

### 工具

- `list_sops()`:无需参数,返回全部 SOP 的 id、名称、简介和触发词;不筛选、不打分、不返回正文。
- `get_sop(skill_id)`:按 id 读取某篇 SOP 的完整正文供模型依据作答;找不到返回可恢复提示。

典型链路:`list_sops` 查看全部目录 → LLM 选择 → `get_sop` 读全文 → 依据正文作答。

加载器首次仍下载并缓存全部 Markdown;按需展开指只把所选正文交给模型,不是按需从 OSS 下载。
工具顺序由提示词引导模型选择,图中没有新增强制路由。

## Skill / SOP 文件格式

```markdown
---
id: travel-reimbursement          # 唯一标识(缺省用文件名)
name: 差旅费报销                    # 人类可读名称
description: 出差交通住宿费用报销    # 供模型选择 / 展示
triggers: [差旅, 出差, 高铁]        # 触发提示词(供模型选择)
---
# 正文即 SOP:流程、材料、注意事项……
```

改动 `sops/` 后调用 `POST /api/v1/admin/sops/reload` 热重载,无需重启。

## 安装

```bash
conda activate qa-agent          # Python 3.12 环境
cd backend
python -m pip install -r requirements.txt
```

## LLM 配置(必需)

```bash
cp .env.example .env
```

填入任意 OpenAI 兼容端点的 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`(默认 DeepSeek)。
**必须配置真实 `LLM_API_KEY`**:ReAct 图依赖模型的 tool-calling 能力,未配置时
`get_llm()` 会直接抛错,不再有离线 fake 兜底。`.env` 含密钥,**切勿提交**。

## 运行

```bash
python -m uvicorn app.main:app --reload
```

- 健康检查:`GET http://127.0.0.1:8000/health` → `{"status":"ok"}`
- 交互文档:`http://127.0.0.1:8000/docs`

### 对话端点

```bash
# 非流式:返回本次引用的 skill 与完整回答
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"出差高铁票怎么报销?"}'
# → {"skill":"travel-reimbursement","content":"..."}
```

```bash
# 流式(SSE):多事件
curl -N -X POST http://127.0.0.1:8000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message":"出差高铁票怎么报销?"}'
```

流式事件顺序:

| event | data | 含义 |
|---|---|---|
| `tool_call` | `{"name","args"}` | agent 发起了一次工具调用(用于前端「活动」展示) |
| `tool_result` | `{"name","tool_call_id"}` | 对应工具执行完毕 |
| `token` | `{"content"}` | **最终答案**的增量文本(工具原料与中间铺垫不在此流) |
| `done` | `{"skill"}` | 收尾,附本次引用的 SOP id(`used_sop_id` 回读,可为 null) |

```bash
# 热重载 SOP 目录
curl -X POST http://127.0.0.1:8000/api/v1/admin/sops/reload
# → {"reloaded": 2}
```

## 测试

```bash
python -m pytest
```

测试**离线、不联网、不接真实 LLM**,覆盖:图能否编译且节点 / 边齐全、SOP 加载器与目录
工具、`used_sop_id` 轨迹回读、流式 SSE 的事件形状(用假图喂合成流)。**真实推理与工具
选择的准确度需配 `LLM_API_KEY` 做端到端冒烟**——离线不覆盖。


## SOP 正文图片

编辑器支持「插入图片」及粘贴截图，上传完成后在光标位置插入 Markdown 图片链接。
支持 PNG/JPEG/GIF/WebP，单文件最大 10 MB；后端通过 Pillow 校验真实图片格式。

- `POST /api/v1/sops/images`：multipart `file`，上传至同一私有桶的 `sops/images/<uuid>.<ext>`（根前缀遵循 OSS_SOPS_PREFIX），返回 `{key, url}`。
- `GET /api/v1/sops/images/{image_id}`：验证图片标识和对象存在性，生成新的 OSS 签名链接并以 307 跳转，禁止缓存跳转响应。
- 正文保存稳定的同源 `/api/v1/sops/images/...` 路径，不保存会过期的 OSS 签名。导出 Markdown 到其他站点时需补上本应用域名。
- 复用现有 SOP 前缀权限；不需要公开桶或把 OSS 凭证放在前端。图片访问沿用本项目当前的无鉴权模式。
- 上传立即生效；取消编辑、删除正文中的图片引用或删除 SOP 不会自动删除 OSS 图片，避免误删共享引用。未引用图片的清理暂未实现。
- 图片可在预览、详情中显示；Agent 可通过 `read_sop_image` 按需查看图片（见下节）。

触发词现作为目录元信息交给模型判断适用性，不再参与关键词命中或相关度打分。


## Agent 查看 SOP 图片与历史回放

工具链：`list_sops()` → `get_sop(skill_id)` → 按需 `read_sop_image(skill_id, image_ids)` → 原聊天模型综合图文回答。模型端点须支持图片型工具消息；已验证项目配置的 DeepSeek 官方 deepseek-flash Chat Completions。

- `skills/images.py` 用 Markdown 解析器提取本应用上传图片的相对路径，支持内联及引用式图片，排除代码块、普通链接、外站 URL。`get_sop` 附带图片 ID 清单。
- 新的图片读取调用直接读取 OSS 当前 SOP 正文，验证 SOP ID、图片 ID、引用归属及原件存在性；每次最多 4 张。
- 工具返回文字与 artifact（版本标识、SOP ID、当时名称、图片 ID、OSS key、说明）。这些稳定身份随工具消息进入 Checkpointer，不保存签名 URL 或 Base64 图片。
- `agent` 每次调用模型前在线程池读取图片原件，检查类型与大小，复制对应 ToolMessage 并临时填入 Base64 `image_url` 内容块；不修改原状态，不插入额外 HumanMessage。最新引用优先，每次最多 8 张、单张 10 MiB、合计 20 MiB 原始图片，重复图片只加载一次。超限或读取失败时向模型明确说明，其他对话继续。
- 历史中已登记的引用不会重新按当前 SOP 验证；替换或删除 SOP 不影响旧会话的图片身份。应用上传使用 UUID 新 key，不覆盖旧图片，且当前不自动清理图片。若人工删除或覆盖 OSS 原件，则不保证历史内容可恢复。
- `/chat` 响应、SSE `done`、历史消息增加 `images` 字段，均使用稳定的应用图片路径。前端展示图集，访问时由现有图片接口重新签名；读取失败显示提示并可重试。API 不回传 Base64。
- 部署需更新 requirements（新增 Markdown 解析依赖）并重建前端。无需增加独立视觉模型或 OCR 配置。

验证：离线测试覆盖引用校验、代码块排除、签名/图片字节不入检查点、SOP 替换/删除后的恢复、缺图降级、大小限制、跨轮隔离以及流式/非流式/历史接口。真实 deepseek-flash 在合成图片与内存存储上完成目录→正文→识图调用链，并在删除测试 SOP 后完成流式续聊；没有操作真实 OSS/Redis 数据。


## 知识库 OCR 提取与索引发布（含扫描件 / 发票）

知识库原件存 OSS、向量存 Qdrant。扫描件、照片、截图没有文本层，需要用户**显式选择一种提取方式**；
不做自动分类、前缀路由或自动切换 —— 每次索引都用当次选择的方式，便于对照费用与效果。

### 五种提取方式

| `extraction_mode` | 含义 | 调用云端 OCR |
|---|---|---|
| `native_only` | 不使用 OCR，只取原生文本层（不产生付费调用） | 否 |
| `general` | 通用文字识别（整页，基础版 `Type=General`） | 是 |
| `general_advanced` | 通用文字识别（整页，高精版 `Type=Advanced`） | 是 |
| `invoice` | 发票识别；可加 `mixed_invoice=true` 表示「混贴票据页」 | 是 |
| `payment_record` | 付款详情识别 | 是 |

- PDF **逐页**处理：每页渲染成图后单独提交识别（不假定一次请求能识别全部页面），
  页号随文本块一起保留，引用可定位到原页。
- 发票 / 付款详情的结构化字段会先转成可检索文本，再走同一条「切块 → 向量化 → Qdrant」管线；
  不建票据字段库、不做财务统计与业务查重。
- 非法组合（`mixed_invoice=true` 配非发票、`refresh_ocr=true` 配 `native_only`）返回 422；
  选了 OCR 方式但没配凭证会明确报错，不会静默按原生提取。
- 识别结果与转换文本分两层缓存进 Redis（识别键只含**图片字节**与调用参数，**不含页码与整份文件哈希**：
  同一张图出现在哪一页、哪个文件都命中同一个键）；**失败与空结果都不写成功缓存**，重跑会重试；
  `refresh_ocr=true` 只强制重新识别，转换结果仍按内容哈希复用（详见下节）。
- `native_only` 下没有文本层的文件只是「跳过」并注明原因，不拖垮整批；而 OCR 方式下
  页面识别失败属于「该有内容没抽全」—— 本次不发布，旧索引继续可用。

### 安全发布与回退

索引不再「先删旧索引」：任务写入新的物理集合（`{QDRANT_COLLECTION}__<UTC 时间>-<随机>`），
校验向量数量 / 维度 / 范围外内容没丢、源文件没被改过之后，才切换 `INDEX_STATE_DIR/index-manifest.json`
里的生效指针；旧版本保留供回退，两代之前的版本自动清理。

- 目标文件失败 → 不发布，旧索引照常检索（明确提示，不悄悄部分替换）。
- 子树 / 单文件任务会把范围外向量原样复制进新版本，范围外内容不受影响；已被删除的文件不会被复活。
- **迁移**：`index-manifest.json` 不存在时读写仍指向 `.env` 的 `QDRANT_COLLECTION`（现有线上索引不受影响），
  第一次任务成功发布后才切换过去。回退：`POST /api/v1/admin/knowledge/index-manifest/rollback`。

### 接口

索引只有一个入口：`POST /index-jobs`（唯一同步入口 `/index`、`/reindex` 已删除 —— 生产未上线，
不留兼容层；单文件索引就是 `scope.kind=keys` 的单元素任务，与前端上传后自动索引、失败重试同一条路径）。

```bash
# 索引任务（202 + 轮询）：scope 用 prefix（子树）或 keys（显式清单），两者互斥；
# 必须显式传 extraction_mode，同一时刻只允许一个任务（否则 409）
curl -X POST http://127.0.0.1:8000/api/v1/admin/knowledge/index-jobs \
  -H "Content-Type: application/json" \
  -d '{"scope":{"kind":"prefix","prefix":"05_日常财务与报销/"},
       "options":{"extraction_mode":"general","mixed_invoice":false,"refresh_ocr":false}}'

# 单文件（等价于上传后的自动索引）：key 为知识库相对路径，不含 OSS_PREFIX
curl -X POST http://127.0.0.1:8000/api/v1/admin/knowledge/index-jobs \
  -H "Content-Type: application/json" \
  -d '{"scope":{"kind":"keys","keys":["05_日常财务与报销/住宿发票.pdf"]},
       "options":{"extraction_mode":"invoice","mixed_invoice":false}}'

curl http://127.0.0.1:8000/api/v1/admin/knowledge/index-jobs/current          # 进行中的任务
curl "http://127.0.0.1:8000/api/v1/admin/knowledge/index-jobs/<job_id>/files?offset=0&limit=100"
curl http://127.0.0.1:8000/api/v1/admin/knowledge/index-manifest              # 当前 / 可回退版本
```

任务状态与明细存 `INDEX_STATE_DIR/jobs/`（单工作者锁 + 心跳）；**服务重启会把未结束的任务标记为中断失败**
（已识别页面都在缓存里，重新排队不会重复计费），前端可恢复进度显示。
`INDEX_STATE_DIR/manifests/` 与 `deletions/` 只放**未收尾的恢复记录**（发布清单、删除清理），
启动时由 `ocr_jobs.reconcile()` 自动补完；已完成即删，内容可重建，不要放进 OSS 桶。

### 配置

见 `.env.example` 的 OCR 段：`OCR_ENGINE=aliyun` + `OCR_ALIYUN_ACCESS_KEY_ID/SECRET/ENDPOINT`
（RAM 子账号只授权 OCR 接口；与 OSS / DashScope 是三套独立凭证）、`INDEX_STATE_DIR`、
`OCR_TIMEOUT_SECONDS` / `OCR_MAX_RETRIES`、`OCR_RENDER_DPI` / `OCR_MAX_PAGE_PIXELS`。

### 测试

`pytest` 离线跑全部用例：OCR 与 Embeddings 都打桩（用内存 OSS + 内存 Qdrant），
覆盖五种模式与非法组合、PDF 逐页与混贴选项、专用字段转文本、缓存命中与 `refresh_ocr`、
失败重试与任务恢复、页码 / 来源保留、索引失败时旧版本仍可用、子树重建不动范围外内容。
**未验证**：真实阿里云 OCR 调用、真实 OSS / Qdrant 上的批量任务（需先配凭证再做小样本验收）。


## 三层结果缓存与文件身份（Redis）

按 [docs/Redis缓存与文件身份管理技术方案.md](../docs/Redis缓存与文件身份管理技术方案.md) 实现。
一个共享 Redis 实例承担三件事（键前缀 `qa:` 隔离）：会话记忆（checkpointer）、待办清单、
以及本节的缓存与文件登记。

### 键与策略

| 层 | 键 | 内容 |
|---|---|---|
| OCR 识别结果 | `qa:cache:ocr:v1:<digest>` | 原始响应（诊断用）；键含图片字节 SHA-256 + 供应商/接口/端点/Type/配置版本 |
| 转换文本 | `qa:cache:text:v1:<digest>` | 正文 + `recognized_fields` 标记 + 告警；键含内容哈希 + 规则版本 + 方式 |
| 向量 | `qa:cache:embedding:v1:<digest>` | **只缓存索引侧的切块**；键含文本 + 端点 + 模型 + 维度 + `EMBEDDINGS_VERSION`（用户问题的向量不落缓存，见下） |
| 计算锁 | `qa:lock:<层>:<digest>` | `SET NX PX` + 随机令牌 + 半程续期 + Lua 校验后释放 |
| 文件身份 | `qa:path:<digest>` → `qa:document:<uuid>:record` / `:manifest` | 路径映射、登记、已发布版本用到的缓存键清单 |

- 结果与清单**无 TTL**；锁有 TTL（`CACHE_LOCK_TTL_SECONDS`，计算中自动续期）。
- **改文本转换规则只 +1 `TEXT_RULES_VERSION`**（[ocr_cache.py](app/rag/ocr_cache.py)）：从识别缓存重新转换，不重新调 OCR；
  改识别参数 / 端点 / 换供应商 +1 `OCR_CACHE_VERSION`。换模型 / 换维度 / 换 `EMBEDDINGS_VERSION` 时向量键自然不同。
- 「未命中」只等于「查询成功但 key 不存在」。连接失败、权限错误、读取失败一律抛 `CacheUnavailable`
  （路由层转 503），**绝不当作未命中继续付费调用**；写入失败带错误类别，`CacheOutOfMemory` 用固定中文文案
  经现有任务错误通道提示，任务停在当前文件、不发布，旧索引继续可用。
- 单飞：未命中先抢锁，持锁后二次读；没抢到的等 `CACHE_LOCK_WAIT_SECONDS`，等不到就抛
  `CacheBusy`（路由层转 503，**可重试的忙碌状态**）—— **绝不在没有锁时自行付费计算**。
  长计算（含批量向量化的多把锁）后台按 TTL 半程续期；释放 / 续期都校验持有者令牌。
  锁过期、进程崩溃、网络超时仍可能重复计算，**不承诺收费接口严格只执行一次**。
- 批量向量化：同批重复文本先去重（只提交一次、共用向量），未命中的抢到锁后整批提交；
  批量路径同样遵守上面的忙碌语义（[embedding_cache.py](app/rag/embedding_cache.py)）。
- 用户查询（检索）**不走缓存**：`search_knowledge` 直接调 embedding 客户端，检索链路的可用性不依赖
  Redis —— 缓存故障时已有知识库照常问答（§4.3 修订；[retrieve.py](app/rag/retrieve.py)）。
- 启动自检：`_require_deps()` / `run_index_job()` 先调 `ensure_available()`，缓存没接通在建任务时就报 503。

### 文件身份与清单

- 后端生成 UUID v4 `document_id`：上传时即登记（`documents.register`），重新索引 / 内容更新保持同一 ID，
  删除后重新上传是新 ID；移动 / 改名提供 `documents.move()` 能力但**不挂 API 与 UI**（当前无此入口，
  直接在 OSS 控制台移动对象不会延续身份）。
- 每份文件的正式清单记「当前已发布版本」用了哪些缓存键；发布成功后删除
  `旧清单键 − 新清单键`（**只删明确列出的键**，不 `FLUSHDB`、不按前缀模糊删除）；
  共享缓存被删的代价是别的文件下次读取按未命中重建，**其他文件的 Qdrant 索引不受影响**。
- 发布中途失败留恢复记录，重试时**逐文件比对当前清单与记录里的快照**：写清单与清缓存两个阶段
  分开推进；若该文件已被更新的发布接管，既不回写旧清单、也只补删现版本不再用的旧键 ——
  过期任务不会覆盖新版本，也不会因为「全库当前版本已变」就跳过该收尾的文件。
- 删除文件分四个阶段（原件 → 向量 → 缓存 → 登记 / 清单 / 路径映射），**先落恢复记录再删原件**：
  记录缺了必要信息（Redis 读不到登记 / 清单）就直接 503、原件不动，不会出现「原件已删却没有清理凭据」；
  删到一半失败时记录留在 `INDEX_STATE_DIR/deletions/`，启动对账按已完成的阶段续做，不重跑 OCR。
  清理只用删除时的快照，清单确实不存在就不猜缓存范围；登记 / 清单 / 路径映射只清仍属于这次删除的部分，
  不会误伤同路径重新上传的新文件。

### 配置与部署要求

`.env`（见 `.env.example`）：`CACHE_BACKEND=redis｜memory｜none`（默认 redis，memory 仅测试，
none = 显式关闭并重复付费调用，仅限本地开发）、共用 `REDIS_URL`、`CACHE_REDIS_TIMEOUT_SECONDS` /
`CACHE_LOCK_TTL_SECONDS` / `CACHE_LOCK_WAIT_SECONDS`、`EMBEDDINGS_VERSION`。

部署侧（**应用不会在启动时修改共享 Redis 的全局配置**）：`maxmemory <容量>` + `maxmemory-policy noeviction`、
开启 AOF（`appendfsync everysec`）与可恢复备份、存储目录持久挂载。达到上限时索引任务暂停并明确报错。
本功能只用 String / `SET NX` / Lua，不需要 RedisJSON + RediSearch（那是 checkpointer 的要求）。

### 迁移与回滚

- **现有文件**：无需一次性迁移。上传时自动登记，历史文件在下次索引（`prepare_file`）时补登记；
  未登记过的文件删除时没有清单 → 只删登记，不猜缓存范围。
- **旧本地 OCR 缓存（`INDEX_STATE_DIR/cache/*/result.json`）**：本机 `backend/data` 不存在、且旧键里没有
  提交图片的字节哈希，无法验证「输入与参数一一对应」，因此**不自动迁移、也不删除**（方案 §11 的保守原则）。
  旧结果只影响是否免费复用一次识别，不影响正确性。
- **回滚**：只切 Qdrant 指针（`/index-manifest/rollback`）不删任何集合。缓存按发布版本清单清理，
  回退版本用过的旧缓存可能已被清掉（按未命中重建，需重新付费识别 —— 这一取舍方案已接受）；
  若因回退产生大量重复调用，可临时 `refresh_ocr=false` 重跑以复用现存缓存。

### 测试

除上面 OCR 段的离线用例，缓存与文件身份另有一组（不碰真实 Redis、不产生付费调用）：
两层缓存命中与差集清理（页码 / 文件哈希不进识别键、规则版本变化只重转换）、失败与空结果不写缓存、
向量维度校验与非法向量不写入、缓存失败 ≠ 未命中、令牌锁与单飞（等锁超时抛 `CacheBusy`
且不产生提交、锁续期、批量去重、等对方写缓存后复用）、登记并发唯一赢家、
发布后清理与失败重试（过期任务不覆盖新清单、跳过文件仍收尾、重复恢复幂等）、
删除闭环（Redis 故障时保留原件并 503、OSS 删除失败撤销记录、清理中断后重启恢复、
同路径重传不被旧删除误伤、目录删除的批量失败保留记录）、
检索在 Redis 故障 / 未接缓存时照常命中且不写向量缓存、内存上限时任务暂停并给出中文文案。
**未验证**：真实 Redis / OSS / Qdrant / 阿里云 OCR 的联调与容量表现（需先配好测试实例再做小样本验收）。


### 删除恢复与计算锁的故障处理

- 删除恢复补删 OSS 原件或 Qdrant 向量前，检查当前路径仍属于记录中的 document_id，且登记内容版本未变化。身份缺失或冲突时保留记录并停止该任务，不按旧路径删除新文件；需要核对冲突后再处理，不能直接清空恢复记录。
- Qdrant 删除失败时保留未完成阶段及恢复记录，启动对账可以重试；只有后续阶段成功后才清理记录。
- 已记录完成的缓存、登记清理阶段不重复执行。
- embedding 批量取锁的异常清理覆盖取锁、等待、缓存复查和计算全过程。忙碌或 Redis 故障时释放本次已取得的锁，不释放其他任务的锁，不绕过锁收费计算。
- 上述身份检查用于应用维护的登记信息；直接绕过应用修改 OSS，以及跨服务检查与删除间的严格原子性，仍需后续对象版本条件删除或操作串行化进一步保障。


## 调用日志与费用统计（MySQL）

按 [docs/调用日志与费用统计技术方案.md](../docs/调用日志与费用统计技术方案.md) 实现。
记录 embedding / OCR 的**真实外部调用**与按配置单价的**估算费用**：
日志主存储是 **MySQL**（Redis 仍只做缓存与会话，Qdrant 仍只做向量），
**不含聊天 LLM 调用**，**不接官方账单**，界面只报「估算费用」。
未配置 `METERING_MYSQL_URL` 时整层静默关闭，不影响 OCR / 索引 / 检索。

### 记什么、怎么记

| 表 | 一行 = | 关键点 |
|---|---|---|
| `call_events` | 一次真实 HTTP 请求 | 每次重试各一行（`call_group` + `attempt_no` + `retry_of` 串链路）；SDK 内部重试记进 `http_attempts`，不虚增行数 |
| `call_event_items` | 一次批量请求覆盖的一个文件 | 金额按文本数比例**分摊**并标注估算；整笔金额只在事件行记一次 |
| `cache_events` | 一批缓存结果 | 三层（`ocr_raw` / `ocr_text` / `embedding`）的命中 / 未命中 / 等待复用 / 去重，**分表不重复计费** |
| `price_config` | 一条价目 | 按 服务 / 供应商 / 模型或 OCR Type / 计费单位 / 币种 + `effective_from` 配置，在「调用与费用 → 估算依据」页面维护 |

口径（前端与接口共用同一说法）：**超时 ≠ 免费**（用量与金额留空、`billing_status=unknown`，
绝不记 0）；用量优先取供应商回报，拿不到才本端计数，**绝不按字数折算 token**；
缺价格 / 缺用量显示「无法估算」+ 原因；金额全用 `Decimal`，不同币种、不同单位**不合并**。
每条事件在发生时刻把命中的价目**快照**进行里，之后改价不重算历史。

### 接口（`/api/v1/admin/metering`）

`GET /calls`（分页 + since/until/service/purpose/status/job_id/document_id 过滤，
缺省最近 7 天、上限 200/页）、`GET /calls/{event_id}`（含分摊与价格快照）、
`GET /summary`（概览 + 持久化健康）、`GET /prices`（当前价目）、
`POST /prices`（新增价目，只增：重复唯一键 → 409）、`DELETE /prices/{id}`（删除误录价目）。
日志接口只读；价目只增 + 删，改价 = 追加一条更晚生效的规则，历史事件按当时的快照估算、不重算。
**权限现状**：后端没有登录态（统一鉴权机制后续引入，见方案 §9）——
在那之前接口只应在受信网络内暴露，**不要把后端直接放到公网**。

### 运行要求与故障行为

- **建表只走迁移**（应用绝不 `create_all`，也不引迁移框架）：用有 DDL 权限的账号执行
  `mysql --default-character-set=utf8mb4 -u qa_migrate -p qa_agent_prod < migrations/0001_create_metering_tables.up.sql`
  （建库语句、迁移账号 / 运行账号分离、本地与 ECS 的地址差异见方案 §9 与
  [migrations/README.md](migrations/README.md)；本仓库的建库与迁移由你在 ECS 上执行）；
- 连接池每 worker 一份（`POOL_SIZE` + `MAX_OVERFLOW`，`pool_pre_ping` + 回收 + 连接/读写超时）；
  后台写出线程与请求线程**各用独立会话**，事务不跨外部调用，入队不阻塞请求；
- 写库失败只把记录落进 `METERING_PENDING_DIR`（原子领取、幂等重放、指数退避、提交成功才删文件），
  **不会**把已成功的 OCR / Embedding 变成业务失败，也**不会**重做任何付费调用；
  连补写文件都写不下才计 `lost` 并告警（接口与前端都会暴露 pending / lost / db_ok）；
  进程崩溃可能丢掉尚未落盘的记录，不承诺严格一次（方案 §11）。

### 配置

`.env`（见 `.env.example`）：`METERING_MYSQL_URL` / `METERING_POOL_*` / 读写超时 /
`METERING_PENDING_DIR` / `METERING_QUEUE_MAX` / `METERING_FLUSH_BATCH` /
`METERING_FLUSH_INTERVAL_SECONDS` / `METERING_PRICE_CACHE_SECONDS`。
价格不内置、不硬编码：`price_config` 为空时所有金额显示「无法估算」。

### 测试

`tests/test_metering_{model,calls,writer,api,migration}.py` 全离线（mock 付费调用；迁移用例把
`migrations/*.sql` 解析成结构再与模型逐项比对），覆盖批量 / 部分命中 / 去重、查询向量、OCR 原始命中不产生外部调用、
失败 / 超时 / 重试、缺用量 / 缺价格、幂等补写、多文件不重复计、分页 / 过滤 / 时间边界 /
统计一致、缓存与费用不重复计、日志写失败不触发重复业务调用。
`tests/test_metering_mysql.py` 是 **MySQL 专属**集成测试（执行迁移 SQL 建表 / 回滚、列类型与精度、
索引、无外键、Decimal 往返、微秒与 UTC 边界、唯一约束与回滚、并发幂等写、真实写出器落盘 / 补写），
**没设 `METERING_TEST_MYSQL_URL` 就整组 skip**——脚本拒绝在不像测试库的库名上建表 / 删表；
`SQLite 通过不等于 MySQL 通过`，无测试库时如实报告为未验证（跑法见方案 §12）。
