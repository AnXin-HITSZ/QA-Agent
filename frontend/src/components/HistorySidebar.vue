<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from "vue";

import { useChat } from "../composables/useChat";
import { useSidebar } from "../composables/useSidebar";

const {
  conversations,
  historyEnabled,
  historyDegraded,
  threadId,
  loading,
  newConversation,
  openConversation,
  removeConversation,
  loadConversations,
} = useChat();

// 切走(新对话 / 打开某通)时通知父级,移动端顺手收起抽屉。
const emit = defineEmits<{ (e: "navigate"): void }>();

// 行内删除确认:点垃圾桶先亮出「删 / 取消」,避免误清 Redis 记忆(不可恢复)。
const confirmingId = ref<string | null>(null);

// 搜索框:目前仅前端显示,内容检索待后端接入(点胶囊「搜索」时聚焦此框)。
const { focusSearchSignal } = useSidebar();
const searchRef = ref<HTMLInputElement | null>(null);
const searchText = ref("");
watch(focusSearchSignal, async () => {
  await nextTick();
  searchRef.value?.focus();
});

onMounted(loadConversations);

function onNew(): void {
  newConversation();
  emit("navigate");
}

async function onOpen(tid: string): Promise<void> {
  await openConversation(tid);
  emit("navigate");
}

async function onDelete(tid: string): Promise<void> {
  confirmingId.value = null;
  await removeConversation(tid);
}

// 今天只显示时间,否则显示月日;非法时间兜底为空。
function when(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}
</script>

<template>
  <aside class="side">
    <div class="side__top">
      <div class="side__search">
        <svg class="side__search-ic" viewBox="0 0 16 16" aria-hidden="true">
          <circle cx="7" cy="7" r="3.5" />
          <line x1="9.6" y1="9.6" x2="13" y2="13" />
        </svg>
        <input
          ref="searchRef"
          v-model="searchText"
          type="search"
          class="side__search-in"
          placeholder="搜索对话内容"
          aria-label="搜索对话内容"
        />
      </div>
      <button class="side__new" type="button" :disabled="loading" @click="onNew">
        <span class="side__plus" aria-hidden="true">＋</span> 新对话
      </button>
    </div>

    <!-- degraded 优先:记忆已启用但这次读不到(超时/Redis 无响应)→ 给方向 + 重试,而非谎称未启用 -->
    <div v-if="historyDegraded" class="side__off side__off--warn">
      <p class="side__off-msg">暂时读不到历史对话。<br />记忆服务无响应,请稍后重试。</p>
      <button class="side__retry" type="button" :disabled="loading" @click="loadConversations">
        重试
      </button>
    </div>

    <nav v-else-if="historyEnabled" class="side__list" aria-label="历史对话">
      <p v-if="!conversations.length" class="side__empty">还没有历史对话。</p>
      <ul v-else class="side__ul">
        <li
          v-for="c in conversations"
          :key="c.thread_id"
          class="side__item"
          :class="{ 'is-active': c.thread_id === threadId }"
        >
          <button
            class="side__open"
            type="button"
            :disabled="loading"
            :title="c.title"
            @click="onOpen(c.thread_id)"
          >
            <span class="side__title">{{ c.title }}</span>
            <span class="side__meta">{{ c.message_count }} 条 · {{ when(c.updated_at) }}</span>
          </button>

          <div class="side__act">
            <template v-if="confirmingId === c.thread_id">
              <button class="side__yes" type="button" @click="onDelete(c.thread_id)">删除</button>
              <button class="side__no" type="button" @click="confirmingId = null">取消</button>
            </template>
            <button
              v-else
              class="side__del"
              type="button"
              aria-label="删除这通对话"
              @click="confirmingId = c.thread_id"
            >
              ✕
            </button>
          </div>
        </li>
      </ul>
    </nav>

    <p v-else class="side__off">跨轮记忆未启用。<br />配置 Redis 后可在此查看历史对话。</p>
  </aside>
</template>

<style scoped>
.side {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--surface-2);
  border-right: 1px solid var(--line);
}
.side__top {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 12px;
  border-bottom: 1px solid var(--line);
}
.side__search {
  display: flex;
  align-items: center;
  gap: 7px;
  height: 36px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  transition: border-color 0.15s;
}
.side__search:focus-within {
  border-color: var(--primary);
}
.side__search-ic {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  fill: none;
  stroke: var(--muted);
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.side__search-in {
  flex: 1;
  min-width: 0;
  border: 0;
  background: transparent;
  font: inherit;
  font-size: 13px;
  color: var(--ink);
  outline: none;
}
.side__search-in::placeholder {
  color: var(--muted);
}
.side__new {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 38px;
  border: 1px solid var(--primary);
  border-radius: var(--radius-sm);
  background: var(--primary-tint);
  color: var(--primary-strong);
  font: inherit;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}
.side__new:hover:not(:disabled) {
  background: color-mix(in srgb, var(--primary-tint) 70%, var(--surface));
  border-color: var(--primary-strong);
}
.side__new:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.side__plus {
  font-size: 16px;
  line-height: 1;
}
.side__list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}
.side__empty,
.side__off {
  margin: 0;
  padding: 18px 14px;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.6;
}
.side__off--warn {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
}
.side__off-msg {
  margin: 0;
}
.side__retry {
  padding: 6px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.side__retry:hover:not(:disabled) {
  border-color: var(--primary);
  color: var(--primary);
}
.side__retry:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.side__ul {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.side__item {
  display: flex;
  align-items: stretch;
  border-radius: var(--radius-sm);
  transition: background 0.12s;
}
.side__item:hover {
  background: var(--surface);
}
.side__item.is-active {
  background: var(--surface);
  box-shadow: inset 3px 0 0 var(--primary);
}
.side__open {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 9px 10px;
  border: 0;
  background: transparent;
  text-align: left;
  font: inherit;
  color: var(--ink);
  cursor: pointer;
}
.side__open:disabled {
  cursor: not-allowed;
}
.side__title {
  font-size: 13.5px;
  line-height: 1.3;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.side__meta {
  font-size: 11.5px;
  color: var(--muted);
}
.side__act {
  display: flex;
  align-items: center;
  gap: 4px;
  padding-right: 6px;
}
.side__del {
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: var(--radius-xs);
  background: transparent;
  color: var(--muted);
  font-size: 13px;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.12s, color 0.12s, background 0.12s;
}
.side__item:hover .side__del,
.side__item.is-active .side__del {
  opacity: 1;
}
.side__del:hover {
  color: var(--seal);
  background: var(--seal-tint);
}
.side__yes,
.side__no {
  height: 24px;
  padding: 0 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-xs);
  background: var(--surface);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.side__yes {
  border-color: color-mix(in srgb, var(--seal) 40%, transparent);
  color: var(--seal);
}
.side__yes:hover {
  background: var(--seal-tint);
}
.side__no:hover {
  border-color: var(--primary);
  color: var(--primary);
}
</style>
