import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

// 开发服务器把 /api/* 代理到后端,前端代码里只写相对路径 /api/...,从根上绕开跨域。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
