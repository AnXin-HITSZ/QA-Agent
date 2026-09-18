# Lab QA Assistant — 前端

Vite + Vue3 + TypeScript 的聊天界面。当前**第一步**:最小可用,调用后端**非流式** `/api/v1/chat`,
展示命中的 Skill 与回答。下一步再换成 SSE 逐字流式。

## 前置
- **Node.js LTS**(自带 npm)。装好后新开终端,`node -v` 能打印版本即可。

## 安装 & 运行
```bash
cd frontend
npm install
npm run dev
```
浏览器打开 http://localhost:5173 。

> 开发服务器已把 `/api/*` 代理到后端 `http://127.0.0.1:8000`(见 `vite.config.ts`),
> 因此请**先起后端**,再起前端:
> ```bash
> cd backend && python -m uvicorn app.main:app --reload
> ```

## 目录结构
```
frontend/
  index.html          入口 HTML
  vite.config.ts      Vite 配置(含 /api 代理)
  tsconfig*.json      TypeScript 配置
  src/
    main.ts           挂载 App
    App.vue           聊天页(输入 → /api/v1/chat → 渲染)
    api.ts            接口类型与调用
    style.css         样式
    env.d.ts          .vue 类型声明
```

## 脚本
- `npm run dev` — 开发服务器(热更新)
- `npm run build` — 类型检查 + 打包到 `dist/`
- `npm run preview` — 本地预览打包结果
