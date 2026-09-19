<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";

import AnswerRecord from "./components/AnswerRecord.vue";
import AppHeader from "./components/AppHeader.vue";
import Composer from "./components/Composer.vue";
import EmptyState from "./components/EmptyState.vue";
import HistorySidebar from "./components/HistorySidebar.vue";
import UserQuery from "./components/UserQuery.vue";
import { useChat } from "./composables/useChat";

const { messages, loading, error, send } = useChat();
const listEl = ref<HTMLElement | null>(null);
// 移动端历史抽屉开关(桌面端侧栏常驻,此值不起作用)。
const sidebarOpen = ref(false);

// 流式时占位回答已在列表里(带自己的进度提示),此时不再显示全局「思考中」。
const isStreaming = computed(() => messages.value.some((m) => m.streaming));
// 逐 token / 工具事件增长时消息条数不变,靠内容与活动总量触发滚动。
const contentLen = computed(() =>
  messages.value.reduce((n, m) => {
    let len = m.content.length;
    for (const s of m.steps ?? []) len += s.text.length + s.tools.length;
    return n + len;
  }, 0),
);

async function scrollToBottom(): Promise<void> {
  await nextTick();
  listEl.value?.scrollTo({ top: listEl.value.scrollHeight, behavior: "smooth" });
}

watch([() => messages.value.length, contentLen, loading], scrollToBottom);
</script>

<template>
  <div class="app" :class="{ 'app--drawer': sidebarOpen }">
    <HistorySidebar class="app__side" @navigate="sidebarOpen = false" />
    <div class="app__backdrop" @click="sidebarOpen = false" />

    <div class="app__main">
      <AppHeader :history-open="sidebarOpen" @toggle-history="sidebarOpen = !sidebarOpen" />

      <main ref="listEl" class="app__thread">
        <div class="thread">
          <EmptyState v-if="!messages.length" @pick="send" />

          <template v-for="(m, i) in messages" :key="i">
            <UserQuery v-if="m.role === 'user'" :text="m.content" />
            <AnswerRecord
              v-else
              :steps="m.steps"
              :skill="m.skill"
              :streaming="m.streaming"
            />
          </template>

          <div v-if="loading && !isStreaming" class="thinking" aria-live="polite">
            <span class="thinking__dot" />
            <span class="thinking__dot" />
            <span class="thinking__dot" />
            <span class="thinking__txt">正在整理答案</span>
          </div>
        </div>
      </main>

      <footer class="app__dock">
        <div class="thread">
          <p v-if="error" class="app__error" role="alert">{{ error }}</p>
          <Composer :loading="loading" @send="send" />
        </div>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.app {
  display: flex;
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
}
.app__side {
  width: 260px;
  flex-shrink: 0;
}
.app__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.app__backdrop {
  display: none;
}
.app__thread {
  flex: 1;
  overflow-y: auto;
  padding: 18px 20px 8px;
}
.thread {
  max-width: var(--maxw);
  margin: 0 auto;
}
.app__dock {
  padding: 12px 20px 18px;
  border-top: 1px solid var(--line);
  background: var(--paper);
}
.app__error {
  margin: 0 0 10px;
  padding: 9px 13px;
  border: 1px solid color-mix(in srgb, var(--seal) 32%, transparent);
  border-radius: var(--radius-sm);
  background: var(--seal-tint);
  color: var(--seal);
  font-size: 13.5px;
}
.thinking {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 12px 2px;
  color: var(--muted);
  font-size: 13.5px;
}
.thinking__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--primary);
  animation: blink 1.2s infinite ease-in-out both;
}
.thinking__dot:nth-child(2) {
  animation-delay: 0.2s;
}
.thinking__dot:nth-child(3) {
  animation-delay: 0.4s;
}
.thinking__txt {
  margin-left: 4px;
}

/* 窄屏:侧栏变为可滑出的抽屉,叠在内容之上 */
@media (max-width: 860px) {
  .app__side {
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    z-index: 20;
    transform: translateX(-100%);
    transition: transform 0.22s ease;
    box-shadow: var(--shadow);
  }
  .app--drawer .app__side {
    transform: translateX(0);
  }
  .app--drawer .app__backdrop {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 15;
    background: color-mix(in srgb, var(--ink) 32%, transparent);
  }
}

@keyframes blink {
  0%,
  80%,
  100% {
    opacity: 0.25;
  }
  40% {
    opacity: 1;
  }
}
@media (prefers-reduced-motion: reduce) {
  .thinking__dot {
    animation: none;
    opacity: 0.5;
  }
  .app__thread {
    scroll-behavior: auto;
  }
  .app__side {
    transition: none;
  }
}
</style>
