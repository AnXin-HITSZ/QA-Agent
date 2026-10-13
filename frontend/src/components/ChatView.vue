<script setup lang="ts">
import { computed, nextTick, onActivated, onDeactivated, ref, watch } from "vue";

import { useChat } from "../composables/useChat";
import { useSidebar } from "../composables/useSidebar";
import { useTodoDrawer } from "../composables/useTodoDrawer";
import { useTodos } from "../composables/useTodos";
import AnswerRecord from "./AnswerRecord.vue";
import Composer from "./Composer.vue";
import EmptyState from "./EmptyState.vue";
import UserQuery from "./UserQuery.vue";

const { messages, loading, error, send, stop, newConversation } = useChat();
// 会话工具胶囊(仅对话视图):边栏开合 / 搜索 / 新对话 / 待办。
const { open: sidebarOpen, toggleDrawer, openSearch, closeDrawer } = useSidebar();
// 右侧待办抽屉开关 + 未完成计数(徽标)。
const { open: todoOpen, toggle: toggleTodos } = useTodoDrawer();
const { openCount: todoOpenCount } = useTodos();
const listEl = ref<HTMLElement | null>(null);

function onNewConversation(): void {
  newConversation();
  closeDrawer();
}

// 开待办抽屉前先收起左侧历史抽屉,一次只展开一侧。
function onToggleTodos(): void {
  closeDrawer();
  toggleTodos();
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

// 自动跟随:只在用户本来就贴着底部时才跟着滚 —— 往前翻旧内容时,新 token 不能把人拽回底部。
const NEAR_BOTTOM_PX = 80;
const stickBottom = ref(true);
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
let lastList: typeof messages.value | null = null;

function atBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;
}

// 只看「位置」不够:程序化平滑滚动同样会派发 scroll 事件,动画途中还没到底,
// 会被误判成用户想往回看。改判方向 —— 往上滑才算脱离跟随,往下滑到底就重新跟随。
let lastTop = 0;
function onScroll(): void {
  const el = listEl.value;
  if (!el) return;
  const top = el.scrollTop;
  const movingUp = top < lastTop;
  lastTop = top;
  if (atBottom(el)) stickBottom.value = true;
  else if (movingUp) stickBottom.value = false;
}

// 「回到最新」是用户明确要求 → 恢复跟随并滚到底(平滑),不再被 stickBottom 拦下。
function jumpToLatest(): void {
  stickBottom.value = true;
  void scrollToBottom();
}

// instant=true 用于逐 token 的跟随:内容本来就在连续长,即时定位最稳,
// 免得一次动画没跑完就被下一次打断(旧写法每 token 一次 smooth,又慢又晕)。
async function scrollToBottom(instant = false): Promise<void> {
  const el = listEl.value;
  if (!el || !stickBottom.value) return;
  await nextTick();
  el.scrollTo({
    top: el.scrollHeight,
    behavior: instant || reduceMotion.matches ? "auto" : "smooth",
  });
}

watch([() => messages.value, contentLen, loading], ([list]) => {
  // 消息数组被整体替换 = 换了一通对话 → 重新回到跟随底部。
  if (list !== lastList) {
    lastList = list;
    stickBottom.value = true;
  }
  void scrollToBottom(isStreaming.value);
});

// 被 KeepAlive 缓存:切走前记下滚动位置,切回时还原(重新挂载 DOM 可能把 scrollTop 归零)。
let savedScroll = 0;
onDeactivated(() => {
  savedScroll = listEl.value?.scrollTop ?? savedScroll;
});
onActivated(() => {
  if (!listEl.value) return;
  listEl.value.scrollTop = savedScroll;
  lastTop = savedScroll;
  stickBottom.value = atBottom(listEl.value); // 还原到的位置就在半中间 → 别跟着滚
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
          aria-controls="history-drawer"
          :aria-expanded="sidebarOpen"
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

      <!-- 待办键:与右侧滑出的待办抽屉同侧,置于工具条右端;图标 / 尺寸 / 悬停同左组 -->
      <div class="tools tools--end" role="group" aria-label="待办">
        <button
          class="tools__btn"
          type="button"
          data-tip="待办清单"
          aria-label="待办清单"
          aria-controls="todo-drawer"
          :aria-expanded="todoOpen"
          @click="onToggleTodos"
        >
          <svg class="tools__ic" viewBox="0 0 16 16" aria-hidden="true">
            <rect x="2.5" y="2.5" width="11" height="11" rx="1.5" />
            <polyline points="5,8 7,10 11,5.5" />
          </svg>
          <span v-if="todoOpenCount" class="tools__badge" aria-hidden="true">{{ todoOpenCount }}</span>
        </button>
      </div>
    </div>

    <main
      ref="listEl"
      class="chat__thread"
      :class="{ 'chat__thread--empty': !messages.length }"
      @scroll.passive="onScroll"
    >
      <div class="thread">
        <EmptyState v-if="!messages.length" @pick="send" />

        <template v-for="m in messages" :key="m.uid">
          <UserQuery v-if="m.role === 'user'" :text="m.content" />
          <AnswerRecord
            v-else
            :steps="m.steps"
            :skill="m.skill"
            :sources="m.sources"
            :images="m.images"
            :streaming="m.streaming"
            :stopped="m.stopped"
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

    <!-- 往回翻看时回答还在下面长:给一条回到底部的路,而不是硬把人拽下去 -->
    <button
      v-if="messages.length && !stickBottom"
      class="chat__jump"
      type="button"
      @click="jumpToLatest"
    >
      回到最新
    </button>

    <footer class="chat__dock">
      <div class="thread">
        <p v-if="error" class="chat__error" role="alert">{{ error }}</p>
        <Composer :loading="loading" @send="send" @stop="stop" />
      </div>
    </footer>
  </div>
</template>

<style scoped>
.chat {
  position: relative; /* 「回到最新」按钮的定位基准 */
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
/* 会话工具:页头「实验室问答」正下方,纯图标、无分隔线;悬停/聚焦显圆形阴影底 + 提示。
   两端对齐:左侧是会话工具组(边栏 / 搜索 / 新对话),右端是待办键 —— 与其右侧滑出的抽屉同侧。 */
.chat__bar {
  position: relative;
  z-index: 2; /* 让提示气泡盖在下方滚动区之上 */
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 24px 2px;
}
.tools {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: -7px; /* 首个图标视觉左缘对齐标题 */
}
/* 右端组:负边距镜像左组,让末尾图标视觉右缘同样对齐内容边 */
.tools--end {
  margin-left: 0;
  margin-right: -7px;
}
/* 最右按钮的提示气泡改为右对齐,免得居中后冒出窗口右缘 */
.tools--end .tools__btn::after {
  left: auto;
  right: 0;
  transform: none;
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
/* 待办键上的未完成计数徽标 */
.tools__badge {
  position: absolute;
  top: 2px;
  right: 2px;
  min-width: 15px;
  height: 15px;
  padding: 0 4px;
  display: grid;
  place-items: center;
  border-radius: 999px;
  background: var(--seal);
  color: #fff;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 9.5px;
  line-height: 1;
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
/* 回到底部:浮在滚动区右下、输入坞之上;纸底细线,不抢回答的视线 */
.chat__jump {
  position: absolute;
  right: 26px;
  bottom: 96px;
  z-index: 3;
  padding: 6px 13px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface);
  color: var(--primary-strong);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
  box-shadow: 0 3px 10px color-mix(in srgb, var(--ink) 14%, transparent);
  transition: border-color 0.15s, color 0.15s;
}
.chat__jump:hover {
  border-color: var(--primary);
  color: var(--primary);
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
