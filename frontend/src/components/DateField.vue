<script setup lang="ts">
import { computed, ref } from "vue";

import { addDaysIso, parseIsoDate, toIsoDate, todayIso } from "../lib/todoDate";

// 通用日期选择器:可选快捷预设 + 自绘月历。受控值为本地日历日字符串 YYYY-MM-DD,null = 未选。
// 展示文案与「是否标红」由调用方以函数传入(待办传相对期限 / 逾期;计量传纯日期)——
// 组件不认识业务语义,口径统一留在调用方的 lib 里。
//
// 月历默认**内联展开**(抽屉这种滚动容器里定位浮层会被裁切);`overlay` 打开时改为
// 绝对定位浮层 —— 页面上的筛选行用它,展开不撑高所在行。
const props = withDefaults(
  defineProps<{
    modelValue: string | null;
    presets?: { label: string; days: number }[];
    format?: (iso: string | null) => string;
    warn?: (iso: string | null) => boolean;
    overlay?: boolean;
  }>(),
  { presets: () => [], overlay: false },
);
const emit = defineEmits<{ (e: "update:modelValue", v: string | null): void }>();

// 月历当前显示的月份(0 基的 m)。
const cursor = ref(monthOf(parseIsoDate(props.modelValue) ?? new Date()));
const open = ref(false);

function monthOf(d: Date): { y: number; m: number } {
  return { y: d.getFullYear(), m: d.getMonth() };
}

// 展开 / 收起月历;每次展开都把视图拉回已选月份(否则上次翻到别处再打开会看不见选中)。
function toggle(): void {
  open.value = !open.value;
  if (open.value) cursor.value = monthOf(parseIsoDate(props.modelValue) ?? new Date());
}

function shift(delta: number): void {
  cursor.value = monthOf(new Date(cursor.value.y, cursor.value.m + delta, 1));
}

// 周一开头、含上下月残余日;行数按当月需要算(5 或 6 行),不硬凑 6 行
// —— 否则像 2026-09 那样会末尾挂一整行全是下月日期,白占高度。
// 每格带 iso / today / sel 三个标记,由计算属性一次算出,模板不再逐格算日期。
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

const presetIso = (days: number): string => addDaysIso(days);
const valueText = computed(() => (props.format ? props.format(props.modelValue) : props.modelValue ?? ""));
const valueWarn = computed(() => Boolean(props.warn?.(props.modelValue)));

// 选中即收起,少一次点击。
function pick(iso: string): void {
  emit("update:modelValue", iso);
  open.value = false;
}
</script>

<template>
  <div class="df" :class="{ 'df--overlay': overlay }">
    <div v-if="presets.length" class="df__presets">
      <button
        v-for="p in presets"
        :key="p.label"
        class="df__chip"
        :class="{ 'is-on': presetIso(p.days) === modelValue }"
        type="button"
        @click="pick(presetIso(p.days))"
      >
        {{ p.label }}
      </button>
    </div>

    <div class="df__row">
      <button
        class="df__datebtn"
        :class="{ 'is-open': open }"
        type="button"
        aria-haspopup="dialog"
        :aria-expanded="open"
        @click="toggle"
      >
        <svg class="df__ic" viewBox="0 0 16 16" aria-hidden="true">
          <rect x="2.5" y="3.5" width="11" height="9.5" rx="1.5" />
          <line x1="2.5" y1="6.5" x2="13.5" y2="6.5" />
          <line x1="5.5" y1="2.2" x2="5.5" y2="4.6" />
          <line x1="10.5" y1="2.2" x2="10.5" y2="4.6" />
        </svg>
        选择日期
        <svg class="df__cv" viewBox="0 0 12 12" aria-hidden="true">
          <polyline points="2.5,4.5 6,8 9.5,4.5" />
        </svg>
      </button>
      <span v-if="valueText" class="df__val" :class="{ 'is-over': valueWarn }">{{ valueText }}</span>
    </div>

    <div v-if="open" class="df__cal" role="dialog" aria-label="选择日期">
      <div class="df__calhd">
        <button class="df__calnav" type="button" aria-label="上个月" @click="shift(-1)">
          <svg viewBox="0 0 14 14" aria-hidden="true"><polyline points="8.5,3 4.5,7 8.5,11" /></svg>
        </button>
        <span class="df__caltitle">{{ cursor.y }}年 {{ cursor.m + 1 }}月</span>
        <button class="df__calnav" type="button" aria-label="下个月" @click="shift(1)">
          <svg viewBox="0 0 14 14" aria-hidden="true"><polyline points="5.5,3 9.5,7 5.5,11" /></svg>
        </button>
      </div>

      <div class="df__calgrid">
        <span v-for="(w, i) in WEEKDAYS" :key="i" class="df__wd" aria-hidden="true">{{ w }}</span>
        <button
          v-for="c in cells"
          :key="c.iso"
          class="df__day"
          :class="{ 'is-out': c.out, 'is-today': c.today, 'is-sel': c.sel }"
          type="button"
          :aria-label="c.iso"
          @click="pick(c.iso)"
        >
          {{ c.day }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.df__presets {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-bottom: 9px;
}
.df__chip {
  height: 30px;
  padding: 0 13px;
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
  transition: border-color 0.14s, background 0.14s, color 0.14s;
}
.df__chip:hover {
  border-color: var(--primary);
  color: var(--primary-strong);
}
.df__chip.is-on {
  border-color: var(--primary);
  background: var(--primary-tint);
  color: var(--primary-strong);
  font-weight: 600;
}
.df__row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.df__datebtn {
  height: 38px;
  padding: 0 12px;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
  transition: border-color 0.14s, background 0.14s, color 0.14s;
}
.df__datebtn:hover {
  border-color: var(--primary);
  color: var(--primary-strong);
}
.df__datebtn.is-open {
  border-color: var(--primary);
  background: var(--primary-tint);
  color: var(--primary-strong);
}
.df__ic {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.df__cv {
  width: 11px;
  height: 11px;
  flex-shrink: 0;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform 0.16s;
}
.df__datebtn.is-open .df__cv {
  transform: rotate(180deg);
}
.df__val {
  font-size: 12.5px;
  color: var(--muted);
}
.df__val.is-over {
  color: var(--seal);
  font-weight: 500;
}

/* 自绘月历:默认内联展开(抽屉内滚动,浮层定位易错位) */
.df__cal {
  margin-top: 10px;
  padding: 11px 11px 9px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
}

/* 浮层模式:相对本组件绝对定位,展开只盖住下方内容、不改变所在行尺寸 */
.df--overlay {
  position: relative;
}
.df--overlay .df__cal {
  position: absolute;
  top: calc(100% + 8px);
  left: 0;
  z-index: 30;
  width: 264px; /* 脱离普通流后须自带宽度(内联时由容器撑开) */
  margin-top: 0;
  box-shadow: var(--shadow);
}
.df__calhd {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 9px;
}
.df__caltitle {
  font-family: "Space Grotesk", "IBM Plex Sans", "Noto Sans SC", sans-serif;
  font-weight: 600;
  font-size: 13.5px;
  color: var(--ink);
}
.df__calnav {
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
.df__calnav:hover {
  background: var(--surface-2);
  color: var(--primary);
}
.df__calnav:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
}
.df__calnav svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.df__calgrid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 2px;
}
.df__wd {
  height: 24px;
  display: grid;
  place-items: center;
  font-size: 11px;
  color: var(--muted);
}
.df__day {
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
.df__day:hover {
  background: var(--surface-2);
}
.df__day:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
}
.df__day.is-out {
  color: color-mix(in srgb, var(--muted) 45%, transparent);
}
.df__day.is-today {
  border-color: var(--primary);
  color: var(--primary-strong);
  font-weight: 600;
}
.df__day.is-sel {
  background: var(--primary);
  border-color: var(--primary);
  color: var(--on-primary);
  font-weight: 600;
}
.df__day.is-sel:hover {
  background: var(--primary-strong);
}
</style>
