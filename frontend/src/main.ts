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

createApp(App).mount("#app");
