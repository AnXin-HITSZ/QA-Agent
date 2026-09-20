import { createApp } from "vue";

// 自托管字体(打包进产物,不请求外网,规避国内 Google Fonts 卡顿)。
import "@fontsource/space-grotesk/500.css";
import "@fontsource/space-grotesk/600.css";
import "@fontsource/space-grotesk/700.css";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";

import App from "./App.vue";
import "./style.css";

const app = createApp(App);

// 全局错误探针:组件在渲染/生命周期/watcher/事件处理器里抛出的异常都会到这里。
// 正常流程一次都不触发;仅在出错时把「哪个组件、哪个阶段、什么异常」打到控制台,
// 便于定位白屏类问题(与各视图内的错误边界互补:边界管界面兜底,这里管完整日志)。
app.config.errorHandler = (err, instance, info) => {
  const opts = (instance?.$options ?? {}) as unknown as Record<string, unknown>;
  const name = (opts.__name as string) || (opts.name as string) || "unknown";
  // eslint-disable-next-line no-console
  console.error(`[app error] <${name}> 于 ${info} 阶段抛出异常:`, err);
};

app.mount("#app");
