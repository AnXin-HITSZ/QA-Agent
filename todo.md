# QA-Agent 待办 & 设计固化

> 本文件记录已确认的设计与"动手前待确认项",便于取到文件后接着落实。
> 约定:一步一步来,不使用工作流。

---

## RAG 知识库摄取(Step 2)—— 等文件到位后落实

### 已确认的决定(2026-09-18)

- **Embeddings**:`text-embedding-v4`(默认 1024 维,OpenAI 兼容端点,代码零改)。限制:单请求 ≤ 10 条输入、单条 ≤ 8192 token → 摄取时分批。
- **部署**:后端最终跑在**云 ECS** → 走**上传接口**,不是本地路径读盘。前端用 `<input type="file" webkitdirectory>` 上传整个文件夹,`webkitRelativePath` 会保留目录树,后端按相对路径的**顶层文件夹名**恢复"类别"。
- **涉密红线**:已确认允许切块原文发 DashScope 云 + 向量片段存云 ECS Qdrant → 维持云方案,不需要本地 embedding / 本地 Qdrant。
- **发票进 RAG**:典型查询是"关于 xxx 的专利发票是否存在?"(存在性/元数据查询)→ 每张发票索引**一张元数据卡片**,不把 OCR 正文硬切进去。

### 管线架构:两轴分派

- **文件扩展名** → 决定用哪个**抽取器**(怎么把文字抽出来)。
- **文件夹名(类别)** → 决定**类别策略**(要不要 OCR、怎么切块、存什么元数据、写进 payload 的 category)。

```
上传(webkitdirectory,保留目录树)
  → 后端按相对路径落盘  knowledge_root/<类别文件夹>/...
重建索引  POST /admin/knowledge/reindex:
  for 每个文件:
      类别   = 相对路径顶层文件夹名     # 语义 → 元数据 + 策略
      cfg    = CATEGORY_REGISTRY[类别] or DEFAULT # 该类别的配置对象
      原文   = EXTRACTORS[扩展名](文件) # 格式 → 抽取器(PDF 再判电子版/扫描件)
      块列表 = cfg.分块策略(原文)           # 类别 → 怎么切
    每块 payload = {text, source文件名, category, page, ...}
      embedding(分批 ≤10) → upsert 进 Qdrant
```

- `CATEGORY_REGISTRY`:代码里一个小字典,文件夹名 → 类别配置;**未知文件夹回退 general**(按扩展名抽、通用切),新建文件夹不会崩。
- `EXTRACTORS`:扩展名 → 抽取函数。PDF 内部**运行时**再判"有文字层就直接抽 / 扫描件转 OCR"(靠内容判断,不靠文件夹)。

### 建议的类别表(取到文件后按实际改)

| 文件夹 | category | OCR | 分块策略 |
|---|---|---|---|
| `patents/` | patent | 视文件(扫描件才 OCR) | 文本切,保权利要求编号 |
| `policies/` `sops/` | policy | 否 | 文本切 |
| `forms/` `tables/` | form | 否 | 表格感知(按行,带表头) |
| `invoices/` | invoice | 是 | 每张发票一张元数据卡片 |
| 其它 | general | 否 | 通用文本切 |

### ⚠️ 动手前待你确认的问题(取到文件后回答)

1. **实际的顶层类别文件夹有哪些?** → 决定 `CATEGORY_REGISTRY`。
2. **专利里扫描件多不多?** → 决定 OCR 是否要早接(阿里云 OCR,和 DashScope 同体系)。
3. **发票元数据从哪来?** → 靠文件名规范抽(简单可靠) / 还是也要 OCR 版面抽字段(更自动、要接 OCR + 版面解析)。

补充:发票这类,**文件命名规范**比 OCR 正文更重要(如 `invoices/某专利名-申请费-2024.pdf`),整理阶段要特别定好。

### 实施顺序(一步一步)

- **Step 2a**:loader 分派骨架 + 上传接口 + `/admin/knowledge/reindex`(v1 先"清空 + 全量重建")。
- **Step 2b**:各类精细抽取(PDF 电子版/扫描件、表格按行、Word)。
- **Step 2c**:发票元数据卡片 +(如需)OCR。
- **Step 3**:检索器 + `search_knowledge` 工具入 `TOOLS` + `used_sources(messages)` → `done` 事件带 `sources`(不加预检索节点,保持 `START→agent⇄tools→END` 纯净,`SYSTEM_PROMPT` 不改)。
- **Step 4**:前端"参考来源"展示 + 上传/重建 UI。
- **后置优化**:更新按钮增量化(记 hash/mtime,只重嵌变更、删除的删向量),替代全量重建。

---

## 记忆系统

### Agent 跨轮对话记忆(Redis checkpointer)—— 后端 DONE(2026-09-18)

- 持久化选 **Redis**(非 Postgres):对话是 JSON、ECS 已装 Redis、LangGraph 有 Redis Saver。
- 用 `langgraph-checkpoint-redis`(0.3.9)的 **`AsyncRedisSaver`**,在 `main.py` 的 FastAPI `lifespan` 里挂进图 → `app.state.graph`;`thread_id` 作会话钥匙,`/chat` 回传、`/chat/stream` 先发 `meta{thread_id}`。
- **优雅降级**:没配 `REDIS_URL` 或 Redis 初始化失败 → 打告警、退回单轮模式,后端照常起。
- **⚠️ 前置(你在 ECS 确认)**:Redis Saver 依赖 **RedisJSON + RediSearch** 模块 → 需 **Redis 8.0+** 或 **Redis Stack**;普通 Redis 会在建索引时报错。
  - 验证:`redis-cli MODULE LIST` 应见 `search` 与 `ReJSON`;不行就 `docker run -d -p 6379:6379 redis:8`。
  - 真 `.env` 填 `REDIS_URL=redis://:密码@<ECS-IP>:6379/0`(设 requirepass + 安全组只放行后端/开发机 IP)。

### 待办

- 后端跑起来后**端到端冒烟**:同一 `thread_id` 连问两轮,验证第二轮记得第一轮上下文。
- **前端**:`thread_id` 存 localStorage + 每次提问回传 + 「新对话」按钮(清 thread_id 开新桶)。
- **下一步:长对话摘要压缩**(消息累积到阈值 → 摘要压缩早期轮次,控制 token / 上下文长度)。

---

## 已完成(参考)

- RAG Step 1 地基:`config.py` / `.env.example` / `app/rag/{embeddings,store}.py`,连通性已端到端验证。
- 环境坑均在启动层解决(`start-dev.sh`):`SSL_CERT_FILE` 指 certifi;VPN 代理导致 502 → 动态 `NO_PROXY`。
