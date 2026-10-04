<script setup lang="ts">
import { computed, ref } from "vue";

import { parseIsoDate, toIsoDate, todayIso } from "../lib/todoDate";

// 自绘月历浮层:待办清单的 DateField 与「时间范围」对话框的组合框共用,月历逻辑只此一份。
// 受控组件,不认识业务语义:只按 modelValue 高亮,点某天只发 pick,由调用方决定收不收起。
//
// 两种形态(与 DateField 原行为一致):
// - 默认内联展开(抽屉等滚动容器里定位浮层会被裁切),由容器撑开宽度;
// - overlay 时绝对定位浮层,贴在最近的定位祖先下沿,展开不撑高所在行。
const props = withDefaults(defineProps<{ modelValue: string | null; overlay?: boolean }>(), {
  overlay: false,
});
const emit = defineEmits<{ (e: "pick", iso: string): void }>();

// 显示月份:已选月份,未选则今天。组件每次展开都是新挂载的(调用方用 v-if),
// 于是「每次展开都回到已选月份」自然成立,不需要向外的控制方法。
const cursor = ref(monthOf(parseIsoDate(props.modelValue) ?? new Date()));

function monthOf(d: Date): { y: number; m: number } {
  return { y: d.getFullYear(), m: d.getMonth() };
}

function shift(delta: number): void {
  cursor.value = monthOf(new Date(cursor.value.y, cursor.value.m + delta, 1));
}

// 周一开头、含上下月残余日;行数按当月需要算(5 或 6 行),不硬凑 6 行
// —— 否则像 2026-09 那样会末尾挂一整行全是下月日期,白占高度。
const cells = computed(() => {
  const first = new Date(cursor.value.y, cursor.value.m, 1);
  const lead = (first.getDay() + 6) % 7; // getDay():0=周日 → 转成周一=0
  const start = new Date(cursor.value.y, cursor.value.m, 1 - lead);
  const daysInMonth = new Date(cursor.value.y, cursor.value.m + 1, 0).getDate();
  const total = Math.ceil((lead + daysInMonth) / 7) * 7;
  const today = todayIso();
  const out: { iso: string; day: number; out: boolean; today: boolean; sel: boolean }[] = [];
  for (let i = 0; i < total; i++) {
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    const iso = toIsoDate(d);
    out.push({
      iso,
      day: d.getDate(),
      out: d.getMonth() !== cursor.value.m,
      today: iso === today,
      sel: iso === props.modelValue,
    });
  }
  return out;
});

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];
</script>

<template>
  <div class="cal" :class="{ 'cal--overlay': overlay }" role="dialog" aria-label="选择日期">
    <div class="cal__hd">
      <button class="cal__nav" type="button" aria-label="上个月" @click="shift(-1)">
        <svg viewBox="0 0 14 14" aria-hidden="true"><polyline points="8.5,3 4.5,7 8.5,11" /></svg>
      </button>
      <span class="cal__title">{{ cursor.y }}年 {{ cursor.m + 1 }}月</span>
      <button class="cal__nav" type="button" aria-label="下个月" @click="shift(1)">
        <svg viewBox="0 0 14 14" aria-hidden="true"><polyline points="5.5,3 9.5,7 5.5,11" /></svg>
      </button>
    </div>

    <div class="cal__grid">
      <span v-for="(w, i) in WEEKDAYS" :key="i" class="cal__wd" aria-hidden="true">{{ w }}</span>
      <button
        v-for="c in cells"
        :key="c.iso"
        class="cal__day"
        :class="{ 'is-out': c.out, 'is-today': c.today, 'is-sel': c.sel }"
        type="button"
        :aria-label="c.iso"
        @click="emit('pick', c.iso)"
      >
        {{ c.day }}
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 内联形态:抽屉里展开,由容器撑开宽度 */
.cal {
  margin-top: 10px;
  padding: 11px 11px 9px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
}

/* 浮层形态:相对最近的定位祖先(组合框 / 筛选行的 DateField)绝对定位 */
.cal--overlay {
  position: absolute;
  top: calc(100% + 8px);
  left: 0;
  z-index: 30;
  width: 264px; /* 脱离普通流后须自带宽度 */
  margin-top: 0;
  box-shadow: var(--shadow);
}
.cal__hd {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 9px;
}
.cal__title {
  font-family: "Space Grotesk", "IBM Plex Sans", "Noto Sans SC", sans-serif;
  font-weight: 600;
  font-size: 13.5px;
  color: var(--ink);
}
.cal__nav {
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: var(--radius-xs);
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.cal__nav:hover {
  background: var(--surface-2);
  color: var(--primary);
}
.cal__nav:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
}
.cal__nav svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.cal__grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 2px;
}
.cal__wd {
  height: 24px;
  display: grid;
  place-items: center;
  font-size: 11px;
  color: var(--muted);
}
.cal__day {
  height: 30px;
  display: grid;
  place-items: center;
  border: 1px solid transparent;
  border-radius: var(--radius-xs);
  background: transparent;
  color: var(--ink);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
  transition: background 0.12s;
}
.cal__day:hover {
  background: var(--surface-2);
}
.cal__day:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
}
.cal__day.is-out {
  color: color-mix(in srgb, var(--muted) 45%, transparent);
}
.cal__day.is-today {
  border-color: var(--primary);
  color: var(--primary-strong);
  font-weight: 600;
}
.cal__day.is-sel {
  background: var(--primary);
  border-color: var(--primary);
  color: var(--on-primary);
  font-weight: 600;
}
.cal__day.is-sel:hover {
  background: var(--primary-strong);
}
</style>
