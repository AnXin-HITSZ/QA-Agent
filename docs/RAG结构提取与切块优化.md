# RAG 结构提取与切块优化

初始实现位于 `codex/rag-improvements` 隔离 worktree，基线提交为
`17fc35412cc4f42a01ac889e7c647f89944fefdb`，随后合入最新主分支并迁移回主项目。
原件内容识别是否完整、正确不属于本轮范围；命名 few-shot 和用户可用的 payload
过滤查询仍是另行待办。

## 提取结构

- Word 按 XML body 顺序读取段落与表格，传播 Heading/标题样式层级；表格行保留空列位置并携带表头。
- XLSX/XLS 传播工作表名称，依据前四行的合并区域检测标题与多层表头；可显式指定表头行数。
  这只是结构规则，不保证任意财务报表都能自动解释。公式仍读取已保存的结果。
- PPTX 每个单元携带幻灯片编号及标题，编号不冒充 PDF 页码。
- `.md`/`.markdown` 通过 markdown-it 保留源行中的列表、表格、代码围栏和块引用，传播 ATX/Setext 标题路径。
- TXT 按空行分段；PDF 保留逐页处理。纯文本标题只做明确的行式标题规则识别，不推断视觉版面。
- 内嵌图片 OCR、公式重算和识别准确性审查未新增。

## 切块与证据

独立 `rag/chunking.py`：同章节/类型的短单元打包；长正文优先句子或换行边界，超长单句才硬切。
表格、代码和列表优先按行切分；代码片段补围栏，Markdown 表格续块重复表头。
长表头超过预算时重复节选并显式标记，原文仍保存在正文片段中。
到达正文末尾即停止，不生成完全包含在前块中的冗余尾块。

切块文本含来源相对路径与章节前缀，前缀算入预算并参与向量化。
payload 新增 `body`、`section_path`、`unit_kind`、`text_start/end`、`offset_version`、
`chunking_version` 及片段框架。偏移指向提取单元经 trim 并按换行连接的规范化文本，
不是原文件字节偏移、页面坐标或可直接高亮的 PDF 位置。策略版本含配置摘要。

检索多取候选后，消除同文件/章节的相同、包含及高度重合片段；保留不同文件的相同正文来源。
主命中先占上下文预算，再补邻块。召回与邻块固定同一个物理集合，不跨原件哈希或章节；
邻块查询失败时保留主命中。相邻 v2 片段用正文偏移去除重复部分。
最终证据可能少于 top_k（过滤、合并或预算限制）；并非 top_k 个不同文件的保证。

PDF 页码、章节路径和切块编号由工具 artifact 经本轮来源聚合进入同步/SSE 响应及历史引用。
前端显示页码和章节，仍链接原件。没有逐句归因或页内高亮；链接的 OSS 最新原件可能晚于索引版本。

## `.env` 参数

修改 `.env` 后重启服务；提取/切块参数变化还需重新索引，已有集合不会自动改变。

| 参数 | 默认值 | 含义 |
|---|---:|---|
| RAG_CHUNK_CHARS | 1000 | 正文块字符预算，含上下文前缀 |
| RAG_TABLE_CHUNK_CHARS | 1400 | 表格块字符预算 |
| RAG_CODE_CHUNK_CHARS | 1600 | 代码块字符预算 |
| RAG_CHUNK_TOKENS | 2048 | UTF-8 字节保守预算，含前缀与框架 |
| RAG_CHUNK_OVERLAP | 150 | 长正文最大重叠字符数 |
| RAG_MIN_BOUNDARY_RATIO | 0.5 | 选择自然断点时的最小填充比例 |
| RAG_DETECT_PLAIN_HEADINGS | true | PDF/TXT 明确章节行识别，可关闭 |
| RAG_EXCEL_HEADER_ROWS | 0 | 0 自动；1–4 显式表头行数 |
| RAG_TOP_K | 5 | 未指定时的主命中数量，最大 20 |
| RAG_SCORE_THRESHOLD | 0.2 | 主命中的余弦相似度阈值 |
| RAG_RETRIEVAL_CANDIDATES | 30 | 去重前候选数量，最大 200 |
| RAG_DUPLICATE_OVERLAP | 0.8 | 同文件/章节区间重合去重阈值 |
| RAG_DUPLICATE_MIN_CHARS | 50 | 包含关系去重的最小正文长度 |
| RAG_NEIGHBOR_RADIUS | 1 | 前后邻块数，0 关闭，最大 3 |
| RAG_CONTEXT_CHARS | 12000 | 单次检索返回正文的总字符预算 |
| RAG_CONTEXT_TOKENS | 16000 | 同一正文的 UTF-8 字节保守预算 |
| RAG_BLANK_PIXEL_LEVEL | 200 | OCR 前灰度深色像素阈值 |
| RAG_BLANK_DARK_RATIO | 0.0005 | 近似空白页的深色像素比例 |
| DOC_CONVERT_TIMEOUT_SECONDS | 90 | 旧版 Word 转换超时 |

OCR_RENDER_DPI、OCR_MAX_PAGE_PIXELS、OCR_TIMEOUT_SECONDS、OCR_MAX_RETRIES 已有配置仍有效。
UTF-8 字节数是字节型分词器的保守上界，不是供应商精确 token 数或账单用量；也不保证任意自定义模型的分词上限。
检索正文和最终格式化证据分别受同一预算约束，后者包含编号、页码与章节标签；超出时标注截断。
工具外层说明、多个检索工具调用累积及整段聊天历史不在这个单次检索预算内。

## 发布保护与迁移

新提取器版本为 2。未重建的旧索引仍可检索，缺少章节/偏移等信息时走兼容路径，无法获得全部新能力。
有旧索引的目标文件若本次变成 skipped（解析失败、空文件或选错提取方式等），本次任务不发布，旧版本继续可用。
新文件的不支持格式仍可跳过。范围外删除的复制向量按实际点数扣减，避免发布数量校验错误。

## 隔离验证与合并

worktree 不复制原项目 `.env`；已另建无凭证的副本 `.env`，使用专用本地状态目录、测试集合名和 OSS 前缀。
Python 专用 venv 只读复用现有 Conda 依赖，前端依赖复制到副本。
测试使用内存 OSS、内存 Qdrant、MemoryCache、假 Embedding/OCR 和副本临时目录。
`kb_env` 同时替换索引函数与 API 预检的 Embedding，避免无凭证副本依赖开发机真实 key。
不启动后端/前端监听，不存在端口冲突。真服务联调另配独立 Redis、测试 Qdrant 集合、OSS 前缀及 MySQL 测试库。

推荐 PowerShell 验证命令（在副本 backend 中）：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
& '../.venv/Scripts/python.exe' -m pytest -p pytest_asyncio.plugin -p anyio.pytest_plugin -q --basetemp=../.rag-test-full
```

合并前等待主项目另一 Agent 提交，先在副本合入主项目新提交并处理配置等重叠文件，再验证组合结果。
不要把未提交主工作区文件直接覆盖进副本，也不要把测试环境或 `.env` 合并回主项目。

## 验证边界

2026-10-12 迁移前已合入主项目 `340c9cc`（最新记忆模块），无文本冲突。
组合版本完整后端验证 **1222 passed / 44 skipped**；前端类型检查及生产构建通过。
主项目未提交的 `todo.md` 与 `LEARNING_PROGRESS.md` 不包含在本次合并中。

2026-10-12 最终离线验证：完整后端 **1177 passed / 43 skipped**，一条 Starlette 依赖弃用告警；
前端 `vue-tsc --noEmit` 与 Vite 生产构建通过。跳过的真数据库/图库与 LibreOffice 用例不计为通过。

新增离线测试覆盖 Word 顺序、Excel 合并表头、Markdown 结构、PPT 标题传播、正文覆盖与边界、
框架预算、邻块版本一致性、去重与引用元数据，以及提取失败保住旧索引。
离线测试不能证明真实语料的语义召回率提高；真实 Embedding/OCR、复杂文件样本及 Redis/Qdrant 远端联调尚需验证。

## 合并后的真实开发环境冒烟（2026-10-12）

主项目已快进至 `d4300f1`，保留原有未提交文档；本机 `.env` 已补齐缺少的 RAG 参数，未覆盖凭证。
获用户授权后执行 `scripts/rag_live_smoke.py --allow-paid`，run_id 为 `e6891f3259ae`。
报告保存在 `backend/data/rag-live-smoke/e6891f3259ae/report.json`（忽略目录，不提交凭证或运行数据）。

- 真实 MySQL 开发库临时账号登录、匿名接口拒绝访问通过。
- 真实 OSS 上传合成 PDF/Markdown，Redis 缓存、Qdrant 独立集合、Embedding、索引任务发布通过。
- 原生索引产生 5 个向量；重复索引点数不变，调用日志确认第二次无付费索引请求。
- 合成图片真实阿里云 OCR 与向量化通过；第二次识别命中缓存，调用日志无新增付费索引请求。
- 真实语义检索返回 PDF 第 1、2 页；LLM 同步回答正确给出 Manager Lin，返回页码来源。
- SSE 正常收到 done 与 PDF 页码；Vite 页面访问和代理至真实后端的登录通过。
- 测试集合、合成原件及其缓存/登记、临时用户与会话/记忆任务已清理；临时服务已停止。
- 索引任务日志合计 4 次外部请求（3 次 Embedding、1 次 OCR），重复任务各 0 次。
  此数不包含查询 Embedding 与聊天 LLM；日志金额是估算，不是官方账单。

脚本使用随机空闲本地端口、独立 OSS 前缀/集合/状态目录，拒绝非开发/测试 MySQL 库。
通过真实 HTTP 路由和认证，但刻意不执行应用全局 lifespan、后台记忆 worker 或共享待办注入，
以免处理其他用户任务；不据此宣称全局启动流程已验收。合成样本也不能代替真实语料质量评测。
