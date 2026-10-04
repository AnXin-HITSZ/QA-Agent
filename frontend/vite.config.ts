import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

// 开发服务器把 /api/* 代理到后端,前端代码里只写相对路径 /api/...,从根上绕开跨域。
//
// 代理目标可用环境变量 QA_AGENT_API_TARGET 覆盖(默认本机 8000):8000 常被 VS Code 的
// 端口转发(ECS)占住,换端口起本地后端时由 start-dev.sh 同步设置这个变量,不用改本文件。
// 刻意不用 VITE_ 前缀:配置只在开发服务器读,不需要(也不应该)进前端产物。
//
// 项目没装 @types/node,这里经 globalThis 读 process,免得编辑器给 vite.config.ts 标红。
const env = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {};

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: env.QA_AGENT_API_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
