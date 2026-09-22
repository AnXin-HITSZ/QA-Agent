<script setup lang="ts">
import { onMounted, ref } from "vue";

import type { Todo } from "../api";
import { useTodoDrawer } from "../composables/useTodoDrawer";
import { useTodos } from "../composables/useTodos";

const { items, enabled, degraded, loading, error, openCount, load, ensureLoaded, add, toggle, remove } =
  useTodos();
const { close } = useTodoDrawer();

// 首挂即拉一次（抽屉常驻 DOM，故应用启动就装填 → 工具键徽标也随之有数）。
onMounted(ensureLoaded);

// 行内删除确认：先亮「删 / 取消」，避免误删。
const confirmingId = ref<string | null>(null);

// 新建表单：折叠态一枚「＋ 新建待办」，展开后填标题 / 分类 / 可选截止。
const adding = ref(false);
const draftTitle = ref("");
const draftCat = ref<"报销" | "其他">("其他");
const draftDue = ref(""); // YYYY-MM-DD;空 = 无截止

function startAdd(): void {
  adding.value = true;
}
function cancelAdd(): void {
  adding.value = false;
  draftTitle.value = "";
  draftCat.value = "其他";
  draftDue.value = "";
}
async function submitAdd(): Promise<void> {
  const title = draftTitle.value.trim();
  if (!title) return;
  const ok = await add({ title, category: draftCat.value, due_date: draftDue.value || null });
  if (ok) cancelAdd(); // 失败保留表单，error 会提示
}

async function onDelete(id: string): Promise<void> {
  confirmingId.value = null;
  await remove(id);
}

function catClass(cat: string): string {
  return cat === "报销" ? "tcat--fee" : "tcat--other";
}

// 截止提示：解析 YYYY-MM-DD，与「今天」比较 → 逾期 / 今天到期 / 剩余天数。
function dueLabel(t: Todo): string {
  const due = t.due_date;
  if (!due) return "";
  const d = new Date(due + "T00:00:00");
  if (Number.isNaN(d.getTime())) return `截止 ${due}`;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((d.getTime() - today.getTime()) / 86400000);
  const md = `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")}`;
  if (diff < 0) return `截止 ${md} · 已逾期 ${-diff} 天`;
  if (diff === 0) return `截止 ${md} · 今天到期`;
  return `截止 ${md} · 还剩 ${diff} 天`;
}

// 逾期高亮：未完成且截止日已过（今天到期不算逾期）。
function isOverdue(t: Todo): boolean {
  if (t.done || !t.due_date) return false;
  const d = new Date(t.due_date + "T00:00:00");
  if (Number.isNaN(d.getTime())) return false;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return d.getTime() < today.getTime();
}
</script>

<template>
  <aside class="td">
    <div class="td__hd">
      <div>
        <div class="td__title">待办清单</div>
        <div v-if="enabled && !degraded" class="td__count">
          {{ openCount }} 项未完成 · 全局共享
        </div>
      </div>
      <button class="td__close" type="button" aria-label="收起待办" @click="close">✕</button>
    </div>

    <!-- degraded 优先:待办已启用但这次读不到(超时/Redis 无响应)→ 给方向 + 重试 -->
    <div v-if="degraded" class="td__off td__off--warn">
      <p class="td__off-msg">暂时读不到待办。<br />待办服务无响应,请稍后重试。</p>
      <button class="td__retry" type="button" :disabled="loading" @click="load">重试</button>
    </div>

    <template v-else-if="enabled">
      <div class="td__body">
        <!-- 新建:折叠按钮 ⇄ 行内表单 -->
        <div class="td__add">
          <button v-if="!adding" class="td__new" type="button" @click="startAdd">
            <span class="td__plus" aria-hidden="true">＋</span> 新建待办
          </button>
          <div v-else class="td__form">
            <input
              v-model="draftTitle"
              class="td__input"
              type="text"
              placeholder="事项，如「差旅报销:上海会议」"
              aria-label="待办事项"
              @keydown.enter.prevent="submitAdd"
            />
            <div class="td__formrow">
              <div class="td__seg" role="group" aria-label="分类">
                <button
                  type="button"
                  :class="{ 'is-active': draftCat === '报销' }"
                  @click="draftCat = '报销'"
                >
                  报销
                </button>
                <button
                  type="button"
                  :class="{ 'is-active': draftCat === '其他' }"
                  @click="draftCat = '其他'"
                >
                  其他
                </button>
              </div>
              <input
                v-model="draftDue"
                class="td__date"
                type="date"
                aria-label="截止日期(可留空)"
              />
            </div>
            <div class="td__formact">
              <button
                class="td__save"
                type="button"
                :disabled="!draftTitle.trim()"
                @click="submitAdd"
              >
                添加
              </button>
              <button class="td__cancel" type="button" @click="cancelAdd">取消</button>
            </div>
          </div>
        </div>

        <p v-if="error" class="td__err" role="alert">{{ error }}</p>

        <p v-if="!items.length" class="td__empty">
          还没有待办。新建一条来记录未完成的报销或事务。
        </p>
        <ul v-else class="td__ul">
          <li
            v-for="t in items"
            :key="t.id"
            class="todo"
            :class="{ 'is-done': t.done }"
          >
            <button
              class="todo__check"
              type="button"
              role="checkbox"
              :aria-checked="t.done"
              :aria-label="t.done ? '标为未完成' : '标为已完成'"
              @click="toggle(t.id)"
            >
              <span v-if="t.done" aria-hidden="true">✓</span>
            </button>
            <div class="todo__main">
              <div class="todo__title">{{ t.title }}</div>
              <div class="todo__meta">
                <span class="tcat" :class="catClass(t.category)">{{ t.category }}</span>
                <span
                  v-if="t.due_date"
                  class="todo__due"
                  :class="{ 'todo__due--over': isOverdue(t) }"
                >
                  {{ dueLabel(t) }}
                </span>
              </div>
            </div>
            <div class="todo__act">
              <template v-if="confirmingId === t.id">
                <button class="todo__yes" type="button" @click="onDelete(t.id)">删除</button>
                <button class="todo__no" type="button" @click="confirmingId = null">取消</button>
              </template>
              <button
                v-else
                class="todo__del"
                type="button"
                aria-label="删除这条待办"
                @click="confirmingId = t.id"
              >
                ✕
              </button>
            </div>
          </li>
        </ul>
      </div>

      <div class="td__foot">
        <p class="td__hint">
          <svg
            class="td__hintic"
            width="14"
            height="14"
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            aria-hidden="true"
          >
            <circle cx="8" cy="8" r="6.2" />
            <line x1="8" y1="7.2" x2="8" y2="11.4" />
            <circle cx="8" cy="4.8" r=".9" fill="currentColor" stroke="none" />
          </svg>
          <span>助手每次回答前都会读取<b>未完成待办</b>,但不会自行改动 —— 由你在这里勾选完成。</span>
        </p>
      </div>
    </template>

    <p v-else class="td__off">待办清单未启用。<br />配置 Redis 后可在此记录未完成事务。</p>
  </aside>
</template>

<style scoped>
.td {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--surface);
  border-left: 1px solid var(--line);
}
.td__hd {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 18px 14px;
  border-bottom: 1px solid var(--line);
}
.td__title {
  font-family: "Space Grotesk", "IBM Plex Sans", "Noto Sans SC", sans-serif;
  font-weight: 600;
  font-size: 15.5px;
  color: var(--ink);
}
.td__count {
  margin-top: 3px;
  font-size: 12px;
  color: var(--muted);
}
.td__close {
  flex-shrink: 0;
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border: 1px solid var(--line);
  border-radius: 50%;
  background: var(--surface);
  color: var(--muted);
  font-size: 15px;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.td__close:hover {
  border-color: var(--primary);
  color: var(--primary);
}
.td__body {
  flex: 1;
  overflow-y: auto;
  padding: 4px 18px 12px;
}

/* 新建:折叠按钮 + 展开表单 */
.td__add {
  padding: 8px 0 4px;
}
.td__new {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  border: 1px dashed var(--line);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--primary);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.td__new:hover {
  border-color: var(--primary);
  background: var(--primary-tint);
}
.td__plus {
  font-size: 15px;
  line-height: 1;
}
.td__form {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
}
.td__input {
  width: 100%;
  height: 34px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-xs);
  background: var(--surface);
  font: inherit;
  font-size: 13px;
  color: var(--ink);
  outline: none;
  transition: border-color 0.15s;
}
.td__input:focus {
  border-color: var(--primary);
}
.td__formrow {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.td__seg {
  display: inline-flex;
  border: 1px solid var(--line);
  border-radius: var(--radius-xs);
  overflow: hidden;
}
.td__seg button {
  padding: 5px 12px;
  border: 0;
  background: var(--surface);
  color: var(--muted);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.td__seg button + button {
  border-left: 1px solid var(--line);
}
.td__seg button.is-active {
  background: var(--primary-tint);
  color: var(--primary-strong);
  font-weight: 600;
}
.td__date {
  height: 30px;
  padding: 0 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-xs);
  background: var(--surface);
  font: inherit;
  font-size: 12.5px;
  color: var(--ink);
  outline: none;
}
.td__date:focus {
  border-color: var(--primary);
}
.td__formact {
  display: flex;
  gap: 8px;
}
.td__save {
  height: 32px;
  padding: 0 16px;
  border: 0;
  border-radius: var(--radius-xs);
  background: var(--primary);
  color: var(--on-primary);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s;
}
.td__save:hover:not(:disabled) {
  background: var(--primary-strong);
}
.td__save:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.td__cancel {
  height: 32px;
  padding: 0 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-xs);
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.td__cancel:hover {
  border-color: var(--primary);
  color: var(--primary);
}
.td__err {
  margin: 8px 0 0;
  padding: 8px 11px;
  border: 1px solid color-mix(in srgb, var(--seal) 32%, transparent);
  border-radius: var(--radius-sm);
  background: var(--seal-tint);
  color: var(--seal);
  font-size: 12.5px;
  line-height: 1.5;
}
.td__empty,
.td__off {
  margin: 0;
  padding: 18px 4px;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.6;
}
.td__off {
  padding: 18px;
}
.td__off--warn {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
  padding: 18px;
}
.td__off-msg {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.6;
}
.td__retry {
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
.td__retry:hover:not(:disabled) {
  border-color: var(--primary);
  color: var(--primary);
}
.td__retry:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 列表 */
.td__ul {
  margin: 6px 0 0;
  padding: 0;
  list-style: none;
}
.todo {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 11px 0;
}
.todo + .todo {
  border-top: 1px solid var(--line);
}
.todo__check {
  flex-shrink: 0;
  margin-top: 1px;
  width: 18px;
  height: 18px;
  display: grid;
  place-items: center;
  border: 1.5px solid var(--line);
  border-radius: var(--radius-xs);
  background: var(--surface);
  color: transparent;
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
  transition: border-color 0.12s, background 0.12s;
}
.todo__check:hover {
  border-color: var(--primary);
}
.todo.is-done .todo__check {
  background: var(--primary);
  border-color: var(--primary);
  color: var(--on-primary);
}
.todo__main {
  flex: 1;
  min-width: 0;
}
.todo__title {
  font-size: 13.5px;
  color: var(--ink);
  line-height: 1.5;
  word-break: break-word;
}
.todo.is-done .todo__title {
  text-decoration: line-through;
  color: var(--muted);
}
.todo__meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 5px;
  flex-wrap: wrap;
}
.tcat {
  padding: 1px 9px;
  border-radius: 999px;
  font-size: 11px;
  line-height: 1.7;
}
.tcat--fee {
  background: var(--primary-tint);
  color: var(--primary-strong);
}
.tcat--other {
  background: var(--surface-2);
  color: var(--muted);
  border: 1px solid var(--line);
}
.todo__due {
  font-size: 11.5px;
  color: var(--muted);
}
.todo__due--over {
  color: var(--seal);
  font-weight: 500;
}
.todo__act {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}
.todo__del {
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
.todo:hover .todo__del {
  opacity: 1;
}
.todo__del:hover {
  color: var(--seal);
  background: var(--seal-tint);
}
.todo__yes,
.todo__no {
  height: 24px;
  padding: 0 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-xs);
  background: var(--surface);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.todo__yes {
  border-color: color-mix(in srgb, var(--seal) 40%, transparent);
  color: var(--seal);
}
.todo__yes:hover {
  background: var(--seal-tint);
}
.todo__no:hover {
  border-color: var(--primary);
  color: var(--primary);
}

/* 只读提示脚注 */
.td__foot {
  padding: 12px 18px 16px;
  border-top: 1px solid var(--line);
  background: var(--surface-2);
}
.td__hint {
  display: flex;
  gap: 8px;
  margin: 0;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.6;
}
.td__hint b {
  color: var(--primary-strong);
  font-weight: 600;
}
.td__hintic {
  flex-shrink: 0;
  color: var(--primary);
  margin-top: 1px;
}

/* 触屏 / 无 hover 设备:删除键常显,免得点不到 */
@media (hover: none) {
  .todo__del {
    opacity: 1;
  }
}
</style>
