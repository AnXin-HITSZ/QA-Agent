# Lab QA Assistant — 后端

实验室问答 + 财务报销 SOP 引导 Agent 的后端。核心机制:**手搭 ReAct agent —— 模型
自行调用工具检索 / 读取 `sops/` 里的 SOP,再依据正文生成分步指引**;日常答疑类问题
直接回答。

- Skill 就是 `backend/sops/` 下的一个 Markdown(frontmatter 记元信息 + 正文写 SOP),
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
      search.py           search_sops(query, top_k):字段加权打分的纯检索函数
    graph/
      state.py            ChatState(messages:add_messages 累积 ReAct 轨迹)
      tools.py            @tool search_sops / get_sop + TOOLS(经 bind_tools 注入)
      nodes.py            agent 节点(LLM 绑定工具 + 行为护栏 SYSTEM_PROMPT)
      builder.py          START → agent ⇄ tools → END(tools_condition 判定)
      trace.py            used_sop_id(messages):从轨迹回读本次引用的 SOP
    api/routes/
      health.py           GET /health
      chat.py             POST /chat、/chat/stream(SSE)、/admin/sops/reload
    schemas/
      chat.py             ChatRequest / ChatResponse
      skill.py            SkillMeta(目录项) / SopHit(检索命中项)
  tests/
    test_health.py · test_graph.py · test_skills.py
    test_search.py · test_trace.py · test_stream.py
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

- `search_sops(query, top_k=5)`:按名称 / 触发词 / id / 简介 / 正文加权打分检索,返回候选
  的 id + 名称 + 简介 + 命中片段。`query` 留空则列出全部 SOP。
- `get_sop(skill_id)`:按 id 读取某篇 SOP 的完整正文供模型依据作答;找不到返回可恢复提示。

典型链路:`search_sops` 发现候选 →(可选)`get_sop` 读全文 → 依据正文作答。

## Skill / SOP 文件格式

```markdown
---
id: travel-reimbursement          # 唯一标识(缺省用文件名)
name: 差旅费报销                    # 人类可读名称
description: 出差交通住宿费用报销    # 供检索打分 / 展示
triggers: [差旅, 出差, 高铁]        # 触发提示词(参与检索打分)
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

测试**离线、不联网、不接真实 LLM**,覆盖:图能否编译且节点 / 边齐全、SOP 加载器与检索
纯函数、`used_sop_id` 轨迹回读、流式 SSE 的事件形状(用假图喂合成流)。**真实推理与工具
选择的准确度需配 `LLM_API_KEY` 做端到端冒烟**——离线不覆盖。
