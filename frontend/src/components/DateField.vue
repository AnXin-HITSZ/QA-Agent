<script setup lang="ts">
import { computed, ref } from "vue";

import { addDaysIso } from "../lib/todoDate";
import CalendarPop from "./CalendarPop.vue";

// 通用日期选择器:可选快捷预设 + 自绘月历(月历本体是与时间范围组合框共用的 CalendarPop)。
// 受控值为本地日历日字符串 YYYY-MM-DD,null = 未选。
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

const open = ref(false);

// 展开 / 收起月历。每次展开都是新挂载的 CalendarPop,视图自然回到已选月份
// (否则上次翻到别处再打开会看不见选中)。
function toggle(): void {
  open.value = !open.value;
}

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

    <CalendarPop v-if="open" :model-value="modelValue" :overlay="overlay" @pick="pick" />
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

/* 浮层模式:相对本组件绝对定位(月历本体在 CalendarPop 内,展开只盖住下方内容、不改变所在行尺寸) */
.df--overlay {
  position: relative;
}
</style>
