# LoCoMo 长期记忆评测操作指南

> 2026-10-09 更新：事件／重要一次性计划、时间精度与消息级来源、共享提取与维护规则、英文评测回答及诊断导出，见 [记忆事件与时间改进及复测说明](记忆事件与时间改进及复测说明.md)。上线需先执行 0008；本轮尚未执行真实迁移或付费复测。

本文只讲一件事:**用 LoCoMo 数据评「长期记忆 vs 无记忆 vs 全文历史」的问答效果,从拿到原始数据到读懂报告,每一步怎么操作。**
系统设计见《长期记忆系统技术方案》§13;通用评测流程(隔离、清理、口径)见《长期记忆系统部署与评测指南》§6;本文是它的 LoCoMo 专章。

> 数据出处:LoCoMo(ACL 2024,`https://github.com/snap-research/locomo`,样本 `data/locomo10.json`),
> 官方仓库为 **CC BY-NC 4.0(仅限非商用)**;本仓库只做**独立实现的数据结构适配**(读其 JSON 格式,
> **未复制官方代码**),适配产物里带出处说明。
>
> **不做的事**:不宣称复现 LoCoMo 官方或 Mem0 论文的任何成绩;本地指标与官方实现有已注明的偏差
> (见 §5),数值只用于「自己的系统 vs 自己」的横向对比。

---

## 1. 什么时候用、什么时候不用

**用**:想量化「记忆模式是否比无记忆更好、离全文历史多远」,并看得分、失败样本与调用花费。

**先别用**:真实付费端点还没配好 / 不能独占环境 / 还没跑过样本冒烟(部署指南 §6.4)。第一版评测
**没有逐题断点恢复**:一轮 ask 中断就整轮重来(构建阶段幂等,重跑不会重复付费)。

---

## 2. 数据准备(prepare,零花费)

`prepare` 是纯文件操作:**不连数据库、不调模型、不花一分钱**,任何机器都能跑。

```sh
# 工作目录:backend/
python scripts/memory_eval.py prepare \
    --input /path/to/locomo10.json \
    --output data/locomo-prepared.json \
    --sample-limit 1        # 先只取第 1 个样本;不带 = 全量 10 个样本
```

输出一份 **schema v2** 评测数据集(格式见部署指南 §6.3),并在 `source.notes` 里给出对账信息:
样本 / 会话 / 消息 / 问题数、类别分布、图片消息数、**对不上的 evidence 引用**(含示例)、
孤立日期键、缺失 / 无法解析的会话时间。**先读这三行再往下跑**——它们是对数据的体检报告。

校验原则是「宁可停,不带半份数据往下跑」:样本 / 消息 / 问题结构、两位说话者、
类别编号、`dia_id` 形态有硬错误直接报错退出;可容忍的脏形态(evidence 对不上、时间缺 / 解析不了)
**不猜也不丢**,原样保留并计数。

---

## 3. 数据怎么映射(人物、时间、顺序、证据、图片)

| 维度 | 映射方式 | 为什么 |
| --- | --- | --- |
| 人物归属 | **一个样本 = 一个主体**:`subject = locomo:<sample_id>`。两位说话者都属于这个主体,记忆合成一份 | 会话双方是同一个人生活史的一部分,跨会话多跳问题(类别 1)的证据天然散在两人各自的表述里;给两人分两个用户会有一半证据召不回 |
| 人物身份不丢 | 每条消息映射为 **user 角色**、正文以「姓名:」开头(如 `Beth: 我养了一只猫`);**不把任何人映射成 assistant** | 映射成 assistant 等于那个人的表述整段消失;角色上用姓名前缀区分,而不是靠「用户 / 助手」 |
| 防止混成一人 | 提取任务随带**适配说明**(`extract_note`,版本 `locomo-extract-note/1`),要求提取结果写出人物姓名、不许用「用户」笼统指代 | 提取提示词默认面向单用户会话;不加说明两位人物的事实会混成一人 |
| 时间 | 会话的原始时间字符串照留(`date_time_raw`);能解析时存 naive ISO(`date_time`,**不带时区**);状态记 `parsed / unparsable / missing`。历史文本与提取输入都带 `[会话记录时间:…]` | 时间类问题(类别 2)要靠时间推理;不猜、不擅自赋 UTC;记录时间与事件时间的区分靠正文自己表述 |
| 顺序 | 会话按 `index` **数字**排序(session_2 先于 session_10,不靠字符串字典序);会话内按消息原顺序 | 时间线是记忆构建的骨架;字符串序是经典坑 |
| 构建顺序 | 一会话一个提取任务,**按历史顺序「登记 → 驱动 worker 到该批收尾 → 再登记下一个」** | 同一时刻的提取与维护决策只涉及一段历史,时序落在编排层,不改正式 worker 调度 |
| evidence | 逐条与消息 `dia_id` 对账:打包(`"D9:1 D4:4"`)、分号、前导零(`"D02:01"`)都归一;对不上的进 `evidence_unresolved`,状态 `ok / partial / unresolved / none`;原始形态照留 | 标准证据是诊断锚点(§6),但不做「压缩记忆证据召回率」——第一版不把 `memory_id` 与 `dia_id` 直接比较 |
| 图片 | **不下载、不识别**;`img_url` / `blip_caption`(数据自带的 BLIP 描述)原样保留,历史文本以 `[分享了图片,图片描述:…]` 附在消息后;三种模式渲染一致 | 第一版任务外;三种模式拿到同样的文本是对的,不然对照就不公平 |
| 不接入 | `observation` / `session_summary` / `event_summary` 字段不进产物(任务外,从 notes 里写明) | 它们是官方数据的辅助标注,接入与否要另立题 |

**题目 → 记忆怎么对上**:每条问题的 `conversation_id` 指向它所属样本,**必须命中**;
未知引用直接报错(不回退到全部历史)——静默回退会把「喂错历史」伪装成正常结果。
`full_context` 模式喂的就是该样本的全部会话原文;`memory` 模式用该样本主体的记忆。

---

## 4. 全流程命令

**先决条件**(build / ask / purge 需要):`.env` 配好 MySQL / Qdrant / Redis / LLM / Embeddings,
`MEMORY_ENABLED=true`、`MEMORY_WRITE_ENABLED=true`;**独占环境**(计量统计按用途 + 时间窗取数,别人的调用会混进来)。

```sh
cd backend

# ① 构建(付费:每个会话一次提取 + 维护决策 + 向量化)
python scripts/memory_eval.py build --dataset data/locomo-prepared.json --run-id locomo-smoke-001

# ② 提问:先不带 --answer(只做召回与证据导出;memory 模式每问一次查询向量化,便宜但不是零)
python scripts/memory_eval.py ask --run-id locomo-smoke-001 --modes no_memory,memory,full_context

# ③ 要真实回答(每问 × 每模式一次生成调用):
python scripts/memory_eval.py ask --run-id locomo-smoke-001 --modes no_memory,memory,full_context --answer

# ④ 本地评分(零调用);judge 显式开启才有付费裁判调用(每答一次裁判调用)
python scripts/memory_eval.py score --run-id locomo-smoke-001
python scripts/memory_eval.py score --run-id locomo-smoke-001 --metrics f1,bleu1,judge --judge-use-answer-model

# ⑤ 报告(纯本地)
python scripts/memory_eval.py report --run-id locomo-smoke-001

# ⑥ 清理(保留 run 目录里的结果文件;不动正式数据、不删集合)
python scripts/memory_eval.py purge --run-id locomo-smoke-001
```

要点:

- `build` 的 `complete` 要**四项全干净**:队列收尾、无失败任务、无待索引、无清理台账欠账;
  否则退出码非 0,且 `ask` 默认拒绝执行。全量 10 个样本约 270+ 段会话,按会话逐个驱动,
  `--max-ticks` 默认 2000 给足;暂时性失败(网络抖动)会按退避重排,加 `--sleep 5` 等它,或稍后
  对同一 run-id 重跑 `build`(幂等)。
- `ask` 的两道闸:必须有 `build.json` 且数据集摘要一致(换数据集不能复用旧记忆);构建必须
  `complete`。诊断性放行加 `--allow-incomplete`,结果标 `run_valid=false` 且退出码非 0。
- 每条问题的标准 evidence 与召回证据都会导出;`--context-chars`(默认 24000)只控制
  `full_context` 的**字符预算检查**(字符数,不折算 token),超过字符预算时标记 skipped，不截断、不调用回答模型。

---

## 5. 评分口径(读数前必看)

| 指标 | 口径 | 边界 |
| --- | --- | --- |
| `f1` | 与官方 `task_eval/evaluation.py` **对齐口径的独立实现**:归一化(去逗号 → 小写 → 去 ASCII 标点 → 去 a/an/the/and → 空白规整)、词计数 F1(交集为空记 0,含两侧都空)、类别 1 两侧按逗号拆多答案取最大再平均、类别 3 取分号前第一段、类别 5 判是否明确说「没有信息」 | **不做词干化**(不引入 nltk):词干化差异可能改变得分，并不保证一律降低;数值**不可与官方结果直接对比**。偏差逐条写在 `scores.json.protocol.f1.deviations` |
| `bleu1` | 本仓库补充的 unigram BLEU(含简短惩罚),官方没有 BLEU | 只用于本仓库内三模式横向对比,不得与论文分数比较 |
| `judge` | **显式开启**(`--metrics f1,bleu1,judge --judge-use-answer-model`)才调用;可独立配置裁判，或使用 `--judge-use-answer-model` 显式复用回答 LLM(不声称更强),模型名 / 温度 / 提示词版本 / 重试次数全部记入 `protocol.judge`;提示词不含模式名;输出经 Pydantic 校验 | 格式错误有限重试;仍失败记 `judge_error`,**不记 0 / 不正确**,不计入准确率分母;类别 5 走「不可回答」分支 |
| 汇总 | 各模式给 `mean`(逐题分数平均)与 `macro`(类别宏平均),两个名字不混用;失败 / 跳过 / 无参考答案**剔除出分母**并单独计数,绝不记 0 | 参考 `scores.json.summary.note` |

**数字之外必须一起读的**:`counts`(有效题数 / 失败 / 跳过)、`failed_question_ids`、
每题状态(`scored / failed / skipped`)与失败原因。一轮里有大量 `failed` 时,任何平均分都不可信。

---

## 6. 报告怎么读

`report --run-id …` 产出 `report.json` 与 `report.md`,重点看四块:

1. **构建有效性**:`complete / drained / jobs_failed / index_pending / cleanup`;未完成的构建后面
   所有数字都不成立(报告开头会有无效警告)。
2. **三模式对照 + 分类别表**:mean 与 macro 各列;分类别看类别 1(多跳)与类别 5(对抗)的差距,
   这两类最能暴露记忆质量问题。
3. **失败样本清单**:问题 id / 模式 / 类型(检索错误、回答调用失败)/ 信息;配合 `results.json` 里
   该题每模式的回答与证据人工定位。
4. **证据诊断**:有标准 evidence 的题数、引用总数、对不上的条数(状态计数);这是**数量口径**的
   诊断,不是证据召回率——第一版不算召回率,也不把 `memory_id` 与 `dia_id` 直接比较。

延迟与 token:每题记 `memory` 检索耗时、各模式回答耗时(memory 模式还记两者之和)、
实际上下文字符数(字符,不折算 token);回答调用的 token 只取模型响应实际返回的 usage,
缺失记 unknown、**不记 0**;计量库口径的 `usage` 是「用途 + 时间窗」过滤,可能混入同窗口其他用户,
也不含回答与裁判调用 —— 三份口径都写在对应文件里,任何一份都不是「完整费用」。

---

## 7. 清理与重跑

```sh
python scripts/memory_eval.py purge --run-id locomo-smoke-001
```

- 只清该 run 的 `scope='eval:<run-id>'`:反查出该作用域里出现过的用户(全量适配是每个样本一个),
  逐个按 `(user_id, scope)` 清记忆行 / 任务行 / 审计正文 / 清理台账,再驱动本作用域 worker
  把清理台账重放到收尾、删该作用域的残留向量点;**不删 Qdrant 集合**(正式索引与评测点同集合),
  不动其他 run,保留 run 目录里的结果文件。`purge.json` 里的 `clean / status / points_left /
  cleanup` 如实反映结果,没清干净退出码非 0。
- 重跑同一 run-id 是安全的:构建入队按幂等键去重,已成功的会话不会重复付费;想彻底重来就
  purge 后换新 run-id(结果目录不会互相覆盖)。

---

## 8. 未验证项与已知局限(先说清楚)

- **真实数据的端到端没跑过**:适配 / 评分 / 报告只在**合成样例**(`scripts/memory_eval.locomo.sample.json`,
  官方结构同形、内容虚构)与离线测试(替身服务)上验过;真实 `locomo10.json` 的脏形态、规模效应、
  真实模型花费都还没有本机数据。第一次务必 `--sample-limit 1` 小样本冒烟。
- **不做逐题断点恢复**:单题失败如实记进结果不拖垮整轮,但中断的 ask 要整轮重跑。
- **词干化偏差**:F1 与官方实现有词干化差异,跨实现不可比。
- **token 统计不完整**:离线无法准确 token 化,上下文用字符数;回答 token 依赖供应商返回。
- **图片未接入识别**;`blip_caption` 以文本进三种模式。
- **不计算压缩记忆的证据召回率**;evidence 只作诊断。
- **单次运行 ≠ 结论**:样本小、温度非零,同一个 run 重跑数字会有波动;要下结论至少固定
  `run.json`(模型 / 参数 / 代码版本)并说明样本量。

---

## 9. 首次小样本冒烟步骤(配齐凭证后照做)

```sh
cd backend

# 1) 适配第 1 个样本,检查 source.notes 的体检数字(零花费)
python scripts/memory_eval.py prepare --input /path/to/locomo10.json \
    --output data/locomo-prepared.json --sample-limit 1 --question-limit 10

# 2) 构建;看 build.json 的 complete 是否为 true、memories 条数是否合理
python scripts/memory_eval.py build --dataset data/locomo-prepared.json --run-id locomo-smoke-001

# 3) 先只做召回(不生成),核对题库与证据导出是否正常
python scripts/memory_eval.py ask --run-id locomo-smoke-001

# 4) 生成回答(付费),然后本地评分;judge 单独一次跑,便于对比「判官与 F1 的分歧」
python scripts/memory_eval.py ask --run-id locomo-smoke-001 --answer
python scripts/memory_eval.py score --run-id locomo-smoke-001
python scripts/memory_eval.py score --run-id locomo-smoke-001 --metrics f1,bleu1,judge --judge-use-answer-model

# 5) 报告,人工抽查失败样本
python scripts/memory_eval.py report --run-id locomo-smoke-001

# 6) 确认无误后再放大(去掉 --sample-limit)或清理
python scripts/memory_eval.py purge --run-id locomo-smoke-001
```

每一步的产物在 `backend/data/eval/runs/locomo-smoke-001/`,清单见部署指南 §6.6。
放大到全量前,先拿这次冒烟核对三样东西:`build.json` 的 `complete`、`results.json` 的
失败题比例、`report.md` 里的代价量级(调用数与耗时)。


## 10. 评测有效性与生产环境边界（修复后）

- `run-id` 在首次构建前绑定数据摘要、构建配置和提取说明版本。只有相同输入与配置才能幂等续建；更改数据或模型配置、执行过 purge 后必须换新的 run-id。首次运行期间不要启动两个相同 run-id 的 CLI 进程。
- `--question-limit 10` 保留完整历史，只减少测试问题，用于验证流程；前十题不是代表性抽样，不能据此报告整体性能。
- 全量历史超过 `--context-chars` 时不发回答请求，评分计为跳过而非零分。字符预算不等于供应商的 token 窗口，仍需合理设置。
- 构建无效的诊断结果禁止评分。重新 ask 会删除旧 scores/report；评分保存 results 的 SHA-256，报告拒绝混用旧评分。
- 类别 1 的 BLEU-1 将所有必要参考项目合并计算，不再取单个项目的最高分；协议为 `qa-agent-bleu1/2`。总体词汇指标用 `mean_f1/mean_bleu1`，是逐题分数算术平均，不称为 pooled micro F1。报告补充总耗时 p50/p95。
- memory 模式留存实际上下文与证据，便于复核。结果包含原始历史、问题、参考答案或记忆正文，应按评测资料管理。

### 独立裁判

默认不自动选择回答模型。独立裁判示例（密钥预先设置在环境变量 `JUDGE_API_KEY`，不要写入命令、报告或代码）：

```sh
python scripts/memory_eval.py score --run-id locomo-smoke-001 --metrics f1,bleu1,judge --judge-model <裁判模型> --judge-base-url <OpenAI兼容地址> --judge-api-key-env JUDGE_API_KEY --judge-temperature 0
```

明确复用回答模型时使用 `--judge-use-answer-model`。裁判输出严格验证布尔类型；提示说明输入是待评数据，不能执行其中的指令。裁判仍需人工抽查。重试耗费额外调用，费用不能只按成功答案数量估算。

### 是否影响生产

逻辑隔离由 `eval:<run-id>` 和派生用户 ID 保证，正式记忆检索与 worker 不读取这份评测数据；评测不写入正式聊天 Checkpointer。隔离不等于资源隔离：复用配置时，共享 MySQL、Qdrant、Redis 缓存、模型额度、并发与限流，费用日志也会进入共用计量库。建议先使用测试 MySQL 数据库、独立 Qdrant 记忆集合及测试 Redis 配置，保持与生产同样的模型和检索参数；勿直接改生产 `.env`。

### 清理验证

```sh
python scripts/memory_eval.py purge --run-id locomo-smoke-001
```

成功必须同时满足退出码 0、`clean=true`、`points_left=0`、`cleanup.pending=0`、`cleanup.failed=0`、errors 为空。不满足时恢复数据库或 Qdrant 后重试同一命令，并检查清理台账错误；不可用删除整个集合的方式替代。

purge 删除该作用域的记忆、来源关联和提取任务，脱敏审计正文，并重放向量删除任务。代次状态和必要清理/审计记录可能保留以防止迟到任务写回。共享 Embedding 缓存和实际调用费用日志不会随 purge 删除；发生过的付费调用不应抹去。本地 run 目录与 prepared 数据集也保留用于复现，确认无需保留后单独删除对应目录和文件。

当前已有 CLI purge，不需要新增前端或 HTTP 删除接口。普通“我的记忆”删除接口针对正式用户，不能用于清评测数据。未来若提供管理员评测控制台，应专门增加管理员权限、固定 eval scope 和清理状态查询。

本次修复只进行了离线回归，未执行真实付费评测或生产删除。
