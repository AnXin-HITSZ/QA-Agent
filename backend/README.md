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
