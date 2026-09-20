<script setup lang="ts">
import { computed, nextTick, onActivated, onDeactivated, ref, watch } from "vue";

import { useChat } from "../composables/useChat";
import { useSidebar } from "../composables/useSidebar";
import AnswerRecord from "./AnswerRecord.vue";
import Composer from "./Composer.vue";
import EmptyState from "./EmptyState.vue";
import UserQuery from "./UserQuery.vue";

const { messages, loading, error, send, newConversation } = useChat();
// 会话工具胶囊(仅对话视图):边栏开合 / 搜索 / 新对话。
const { toggleDrawer, openSearch, closeDrawer } = useSidebar();
const listEl = ref<HTMLElement | null>(null);

function onNewConversation(): void {
  newConversation();
  closeDrawer();
}

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

// 被 KeepAlive 缓存:切走前记下滚动位置,切回时还原(重新挂载 DOM 可能把 scrollTop 归零)。
let savedScroll = 0;
onDeactivated(() => {
  savedScroll = listEl.value?.scrollTop ?? savedScroll;
});
onActivated(() => {
  if (listEl.value) listEl.value.scrollTop = savedScroll;
});
</script>

<template>
  <div class="chat">
    <div class="chat__bar">
      <div class="tools" role="group" aria-label="会话工具">
        <button
          class="tools__btn"
          type="button"
          data-tip="打开边栏"
          aria-label="打开历史边栏"
          @click="toggleDrawer"
        >
          <svg class="tools__ic" viewBox="0 0 16 16" aria-hidden="true">
            <rect x="2.5" y="3.5" width="11" height="9" rx="1.5" />
            <line x1="6" y1="3.5" x2="6" y2="12.5" />
          </svg>
        </button>
        <button
          class="tools__btn"
          type="button"
          data-tip="搜索对话"
          aria-label="搜索对话"
          @click="openSearch"
        >
          <svg class="tools__ic" viewBox="0 0 16 16" aria-hidden="true">
            <circle cx="7" cy="7" r="3.5" />
            <line x1="9.6" y1="9.6" x2="13" y2="13" />
          </svg>
        </button>
        <button
          class="tools__btn"
          type="button"
          data-tip="开启新对话"
          aria-label="开启新对话"
          :disabled="loading"
          @click="onNewConversation"
        >
          <svg class="tools__ic" viewBox="0 0 16 16" aria-hidden="true">
            <line x1="8" y1="3.5" x2="8" y2="12.5" />
            <line x1="3.5" y1="8" x2="12.5" y2="8" />
          </svg>
        </button>
      </div>
    </div>

    <main
      ref="listEl"
      class="chat__thread"
      :class="{ 'chat__thread--empty': !messages.length }"
    >
      <div class="thread">
        <EmptyState v-if="!messages.length" @pick="send" />

        <template v-for="(m, i) in messages" :key="i">
          <UserQuery v-if="m.role === 'user'" :text="m.content" />
          <AnswerRecord
            v-else
            :steps="m.steps"
            :skill="m.skill"
            :sources="m.sources"
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

    <footer class="chat__dock">
      <div class="thread">
        <p v-if="error" class="chat__error" role="alert">{{ error }}</p>
        <Composer :loading="loading" @send="send" />
      </div>
    </footer>
  </div>
</template>

<style scoped>
.chat {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
/* 会话工具:页头「实验室问答」正下方,纯图标、无分隔线;悬停/聚焦显圆形阴影底 + 提示 */
.chat__bar {
  position: relative;
  z-index: 2; /* 让提示气泡盖在下方滚动区之上 */
  flex-shrink: 0;
  padding: 6px 24px 2px;
}
.tools {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: -7px; /* 首个图标视觉左缘对齐标题 */
}
.tools__btn {
  position: relative;
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  transition: color 0.15s, background 0.15s, box-shadow 0.15s;
}
.tools__btn:hover:not(:disabled),
.tools__btn:focus-visible {
  color: var(--primary);
  background: var(--surface);
  box-shadow: 0 2px 8px color-mix(in srgb, var(--ink) 16%, transparent);
}
.tools__btn:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
.tools__btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.tools__ic {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
/* 提示气泡:悬停 / 键盘聚焦时出现在图标下方 */
.tools__btn::after {
  content: attr(data-tip);
  position: absolute;
  top: calc(100% + 4px);
  left: 50%;
  transform: translateX(-50%);
  padding: 4px 8px;
  border-radius: var(--radius-xs);
  background: var(--ink);
  color: var(--paper);
  font-size: 12px;
  line-height: 1.2;
  white-space: nowrap;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.12s;
  z-index: 10;
}
.tools__btn:hover:not(:disabled)::after,
.tools__btn:focus-visible::after {
  opacity: 1;
}
.chat__thread {
  flex: 1;
  overflow-y: auto;
  padding: 8px 20px 8px;
}
/* 无消息时:邀请块在空画布里垂直 + 水平居中(不再浮在偏上) */
.chat__thread--empty {
  display: flex;
  align-items: center;
  justify-content: center;
}
.chat__thread--empty .thread {
  margin: auto;
}
.thread {
  max-width: var(--maxw);
  margin: 0 auto;
}
.chat__dock {
  padding: 12px 20px 18px;
  border-top: 1px solid var(--line);
  background: var(--paper);
}
.chat__error {
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
  .chat__thread {
    scroll-behavior: auto;
  }
}
</style>
