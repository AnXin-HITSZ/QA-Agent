# 知识库 RAG 的 Ragas 评测

## 当前状态

已提供 `backend/scripts/rag_eval.py`：采集与评分分开运行，不把 Ragas 安装到服务环境。
固定 Ragas 0.4.3 与 langchain-community 0.3.31，后者避免新版移除 legacy VertexAI 导入导致 Ragas 无法加载。
采用 0.4.3 支持的 `evaluate` / `LangchainLLMWrapper` 兼容入口，有弃用提示；升级 Ragas 时需重新验证适配。

2026-10-13 只读检查：当前开发配置的生效 Qdrant 集合为 0 点，1024 维，OSS 有 37 份原件。
直接评测这个索引会得到空库表现，不能用于评价新切块策略。正式评测采用用户确认的真实资料快照，
先在独立集合建立索引，再采集回答、评分；原知识库不变。

已准备真实评测草案：`backend/data/rag-eval/real-draft-20261013/review.md` 与 `dataset.pending.json`。
来自 3 份 Word/PPT 制度文件，10 道可回答题、2 道无答案控制题，附原文依据与原件 SHA-256。
标记为 `review_status=pending`，等待用户确认；这些是三文件局部基准，并非整个 37 文件库的代表性成绩。
用户随后说明语料尚未全部上传：草案已改为 `waiting_for_corpus_upload`，仅作参考；
正式题集和标准答案等全部文件上传后重新确定，期间不建立正式评测索引或执行正式评分。
所有候选答案只解释指定 2024/2025 文件内容，不据此断言它们是最新制度。

## 指标与口径

| 指标 | 含义 | 所需标注 |
|---|---|---|
| Context Precision | 支持标准答案的检索片段是否排在前面，衡量排序中的相关证据 | 标准答案 |
| Context Recall | 标准答案中的事实是否能由检索上下文支持；不是直接按文件数量计算的召回率 | 标准答案 |
| Faithfulness | 回答中的声明是否得到实际检索上下文支持 | 无需标准答案 |
| Factual Correctness | 回答与标准答案的事实一致性，默认 F1 | 标准答案 |

另记录期望来源命中率、是否调用检索、是否有上下文、采集失败率与整轮延迟。
没有把四个分数混成一个“总体准确率”。未实现 Answer Relevancy（该指标还需嵌入与额外生成），
后续可增加；也未把 SOP、事务、长期记忆混入知识库的检索质量。

每题输入：`id`、`user_input`、`reference`、`answerable`，可附 `expected_sources`、`category` 和原文证据。
标准答案不能来自待测系统自己的回答；本草案来自指定原件并等待人工确认。
参考答案只用于裁判，不加入被测 Agent 的提示词或索引。

采集走实际 ReAct 图，但无用户长期记忆、待办、已认证事务写入或会话 checkpoint。
上下文取自本题真实 `search_knowledge` ToolMessage 中模型实际看到的文本，而不是事后重新检索，
也不是可能未完全呈现的 artifact。按实际编号片段分组，保留原检索顺序。
使用 SOP 正文/图片或事务工具的题会标记为不适用于 RAG-only 评分，避免缺少辅助证据导致误判。

评分失败/NaN 不记 0，也不隐藏：均值同时报告有效数、失败数、不适用数与总数。
无答案题不进入这四项的均值，需要单独做拒答/编造检查和人工复核。
仅重试失败指标可用 `--retry-failed`；成功指标复用，追加日志按最新题目记录汇总，避免重复计数。

## 安装

项目服务仍使用原 `qa-agent` Python。独立评分环境可以只读复用其已有依赖；新依赖写入自己的 venv。

```powershell
# 在项目根目录
& 'D:/Downloads/Anaconda/anaconda/envs/qa-agent/python.exe' -m venv --system-site-packages .venv-ragas
& '.venv-ragas/Scripts/python.exe' -m pip install -r backend/requirements-ragas.txt
```

脚本关闭 Ragas/HuggingFace 遥测与 LangChain 外部追踪。模型评分仍需将题目、参考答案与上下文发送到配置的裁判端点。
裁判默认使用已有 LLM 配置，温度为 0。可在进程环境或 `.env` 配置
`RAGAS_JUDGE_MODEL`、`RAGAS_JUDGE_BASE_URL`、`RAGAS_JUDGE_API_KEY`；本次未替用户改这些值。
同一个模型生成与裁判可能带来偏差，正式评测应选择固定裁判并结合人工复核。

## 正式真实资料评测

用户确认后修改草案中的问题和答案，将根级 `review_status` 设为 `approved`，并保存为批准版 JSON。
原件快照路径相对 JSON 所在目录解析，移动 JSON 时需一同移动 documents 目录。
入口拒绝未批准的真实集，也拒绝与标注哈希不一致的原件快照。

```powershell
# 以下在 backend 目录运行；会产生真实 Embedding/生成/裁判费用。
& 'D:/Downloads/Anaconda/anaconda/envs/qa-agent/python.exe' scripts/rag_eval.py isolated `
  --dataset data/rag-eval/real-draft-20261013/dataset.approved.json `
  --out data/rag-eval/real-run-001 --limit 12 --allow-paid

& '../.venv-ragas/Scripts/python.exe' scripts/rag_eval.py score `
  --records data/rag-eval/real-run-001/records.jsonl `
  --out data/rag-eval/real-run-001/scoring --allow-paid
```

`isolated` 对快照原件使用现有原生提取和切块策略，真实 Qdrant/Embedding，内存源仓库与内存缓存，
不上传真实文件到另一个 OSS 前缀、不写原 Redis，也不处理后台任务。运行结束删除自己的随机版本集合。
如果希望测当前已发布索引，可以使用 `collect`，它要求索引已存在并固定本次物理集合，不负责建索引。
比较策略参数时，同一批准集、原件快照、生成模型、裁判和指标必须保持一致，分别使用新输出目录。

## 产物与复现

- `dataset.json`：本次实际题目和参考答案。
- `manifest.json`：代码提交、依赖、策略参数、原件/数据集哈希、模型和索引版本。
- `records.jsonl`：逐题回答、模型可见上下文、来源、工具调用、状态与延迟。
- `scoring/judge.json`：裁判、依赖、输入哈希与指标。
- `scoring/scores.jsonl`：逐题逐指标结果，失败重试保留历史。
- `scoring/report.json` / `report.md`：均值及覆盖率，便于人工定位失败题。
- `cleanup.json`：仅属于本次实验的集合清理结果。

产物放在忽略的 `backend/data` 目录；不导出密钥、签名 URL 或其他用户记忆。真实原文与回答仅供本地评测复核。
模型调用具有随机性，数据规模、题型分布、近重复文件和裁判偏差都会影响结果，不能只看高分或只报均值。
当前引用是检索文件/页码列表，不是逐句证据对齐，Ragas 分数也不会自动验证引用页码正确性。

## 已完成的流程验证

`scripts/rag_eval.sample.json` 是人工编写的合成集，不来自正式资料。
运行目录为 `backend/data/rag-eval/synthetic-20261013-pilot`，仅采集前两题；独立测试集合已清理。
四项 Ragas 指标已实际调用裁判，失败的 Context Precision 单项重试后取得有效结果。
这只能说明采集、评分与失败恢复能够运行，不能代表真实知识库质量。

离线评测完整性及相关检索回归：44 项通过。正式真实资料尚未评分，等待全部语料上传后重新确定题集。

## 官方依据

- [Ragas 简单 RAG 评测指南](https://docs.ragas.io/en/stable/getstarted/rag_eval/)
- [Context Precision](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/)
- [Context Recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)
- [Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)
