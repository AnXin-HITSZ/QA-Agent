# Lab QA Assistant — 后端

实验室问答 + 财务报销 SOP 引导 Agent 的后端。当前处于**第一步(已精简)**:
最小后端骨架 + 配置。LLM 不再手写适配层,改由 `.env` 驱动、在**第二步**随 LangGraph
用 LangChain 的 `ChatOpenAI` 创建(OpenAI 兼容端点,换厂商只改 `.env`)。

## 目录结构

```
backend/
  app/
    main.py             FastAPI 入口(CORS + 路由)
    config.py           配置(读取 .env,含 LLM 连接参数)
    api/routes/
      health.py         GET /health
    schemas/            (预留,第二步放对话请求/响应模型)
  tests/
    test_health.py
  requirements.txt · .env.example · pytest.ini
```

## 安装

```bash
conda activate qa-agent          # Python 3.12 环境
cd backend
python -m pip install -r requirements.txt
```

## 运行

```bash
python -m uvicorn app.main:app --reload
```

- 健康检查:`GET http://127.0.0.1:8000/health` → `{"status":"ok"}`
- 交互文档:`http://127.0.0.1:8000/docs`

## LLM 配置(第二步生效)

```bash
cp .env.example .env
```

填入任意 OpenAI 兼容端点的 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`。
离线开发/测试时,第二步会用 LangChain 的 `GenericFakeChatModel` 代替真实模型,无需 Key。

## 测试

```bash
python -m pytest
```
