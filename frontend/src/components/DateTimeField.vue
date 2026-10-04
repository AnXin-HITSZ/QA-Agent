<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from "vue";

import CalendarPop from "./CalendarPop.vue";

// 「日期 + 时刻」组合框(时间范围对话框用):一个 38px 边框盒,左半区开日历、右半区开时刻,
// 中间一条 1px 分隔线。未选日期时左半区显示占位「选择日期」(灰) —— 框恒为整行宽,
// 选没选日期都不产生位移(这正是把日期与时刻并进一个框的原因)。
//
// 浮层都贴在框上:日历贴左边(左半区对齐),时刻贴右边(右半区对齐);点浮层外或 Esc 收起。
// 受控组件:日期 / 时刻分别 v-model:date / v-model:time,业务口径(能否为空、校验)留在调用方。
const props = withDefaults(
  defineProps<{
    date: string | null;
    time: string;
    eod?: boolean; // 时刻浮层页脚是否给「当天 23:59」(结束时间才有)
  }>(),
  { eod: false },
);
const emit = defineEmits<{
  (e: "update:date", v: string | null): void;
  (e: "update:time", v: string): void;
}>();

const HOURS = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, "0"));
const MINUTES = Array.from({ length: 60 }, (_, i) => String(i).padStart(2, "0"));

const open = ref<"cal" | "tf" | null>(null);
const root = ref<HTMLElement | null>(null);
const colH = ref<HTMLElement | null>(null);
const colM = ref<HTMLElement | null>(null);

const hh = computed(() => props.time.slice(0, 2));
const mm = computed(() => props.time.slice(3, 5));

function outside(e: Event): void {
  if (!root.value?.contains(e.target as Node)) closePops();
}

function closePops(): void {
  open.value = null;
  document.removeEventListener("pointerdown", outside, true);
}

// Esc 先收浮层;开着浮层时不能让这次按键穿透去触发 <dialog> 的「按 Esc 关窗」默认动作。
function onEscape(e: KeyboardEvent): void {
  if (!open.value) return;
  e.preventDefault();
  closePops();
}

function toggleCal(): void {
  if (open.value === "cal") {
    closePops();
    return;
  }
  open.value = "cal";
  document.addEventListener("pointerdown", outside, true);
}

function toggleTf(): void {
  if (open.value === "tf") {
    closePops();
    return;
  }
  open.value = "tf";
  document.addEventListener("pointerdown", outside, true);
  void nextTick(centerColumns);
}

// 把选中项滚到列中间(挂载 / 页脚预设动作后调用;逐项点选不动滚动位置,免得跟手指抢)。
function centerColumns(): void {
  for (const col of [colH.value, colM.value]) {
    const el = col?.querySelector<HTMLElement>(".tf__item.is-sel");
    if (col && el) col.scrollTop = el.offsetTop - col.offsetTop - col.clientHeight / 2 + el.offsetHeight / 2;
  }
}

function pickDate(iso: string): void {
  emit("update:date", iso);
  closePops(); // 选中即收起(与 DateField 一致)
}

function setTime(v: string): void {
  emit("update:time", v);
}

function setNow(): void {
  const n = new Date();
  setTime(`${String(n.getHours()).padStart(2, "0")}:${String(n.getMinutes()).padStart(2, "0")}`);
  void nextTick(centerColumns);
}

function setEod(): void {
  setTime("23:59");
  void nextTick(centerColumns);
}

onBeforeUnmount(() => document.removeEventListener("pointerdown", outside, true));
</script>

<template>
  <div ref="root" class="dtf" :class="{ 'is-open': open !== null }" @keydown.escape="onEscape">
    <button
      class="dtf__date"
      :class="{ 'is-open': open === 'cal' }"
      type="button"
      aria-haspopup="dialog"
      :aria-expanded="open === 'cal'"
      aria-label="选择日期"
      @click="toggleCal"
    >
      <svg class="dtf__ic" viewBox="0 0 16 16" aria-hidden="true">
        <rect x="2.5" y="3.5" width="11" height="9.5" rx="1.5" />
        <line x1="2.5" y1="6.5" x2="13.5" y2="6.5" />
        <line x1="5.5" y1="2.2" x2="5.5" y2="4.6" />
        <line x1="10.5" y1="2.2" x2="10.5" y2="4.6" />
      </svg>
      <span class="dtf__val" :class="{ 'is-empty': !date }">{{ date ?? "选择日期" }}</span>
      <svg class="dtf__cv" viewBox="0 0 12 12" aria-hidden="true">
        <polyline points="2.5,4.5 6,8 9.5,4.5" />
      </svg>
    </button>

    <span class="dtf__sp" aria-hidden="true"></span>

    <button
      class="dtf__time"
      :class="{ 'is-open': open === 'tf' }"
      type="button"
      aria-haspopup="dialog"
      :aria-expanded="open === 'tf'"
      aria-label="选择时刻"
      @click="toggleTf"
    >
      <svg class="dtf__ic" viewBox="0 0 16 16" aria-hidden="true">
        <circle cx="8" cy="8" r="5.6" />
        <polyline points="8,4.8 8,8 10.4,9.6" />
      </svg>
      <span class="tf__val">{{ time }}</span>
      <svg class="dtf__cv" viewBox="0 0 12 12" aria-hidden="true">
        <polyline points="2.5,4.5 6,8 9.5,4.5" />
      </svg>
    </button>

    <CalendarPop v-if="open === 'cal'" :model-value="date" overlay @pick="pickDate" />

    <div v-if="open === 'tf'" class="tf__pop" role="dialog" aria-label="选择时刻">
      <div class="tf__hd"><span>时</span><span>分</span></div>
      <div class="tf__cols">
        <div ref="colH" class="tf__col">
          <button
            v-for="h in HOURS"
            :key="h"
            class="tf__item"
            :class="{ 'is-sel': h === hh }"
            type="button"
            @click="setTime(`${h}:${mm}`)"
          >
            {{ h }}
          </button>
        </div>
        <div ref="colM" class="tf__col">
          <button
            v-for="m in MINUTES"
            :key="m"
            class="tf__item"
            :class="{ 'is-sel': m === mm }"
            type="button"
            @click="setTime(`${hh}:${m}`)"
          >
            {{ m }}
          </button>
        </div>
      </div>
      <div class="tf__foot">
        <button v-if="eod" class="dtf__link" type="button" @click="setEod">当天 23:59</button>
        <span v-else></span>
        <button class="dtf__link" type="button" @click="setNow">现在</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 组合框:两个热区共用一个边框,中间细分隔线 */
.dtf {
  position: relative;
  display: flex;
  align-items: stretch;
  width: 100%;
  height: 38px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  transition: border-color 0.14s;
}
.dtf:hover {
  border-color: var(--primary);
}
.dtf__date,
.dtf__time {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 0 12px;
  border: 0;
  background: transparent;
  color: var(--ink);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
  transition: background 0.14s, color 0.14s;
}
.dtf__date {
  flex: 1 1 auto;
  min-width: 0;
  border-radius: var(--radius-sm) 0 0 var(--radius-sm);
}
.dtf__date .dtf__cv {
  margin-left: auto;
}
.dtf__time {
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}
.dtf__date:hover,
.dtf__time:hover {
  background: var(--surface-2);
  color: var(--primary-strong);
}
.dtf__date.is-open,
.dtf__time.is-open {
  background: var(--primary-tint);
  color: var(--primary-strong);
}
.dtf__date:focus-visible,
.dtf__time:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: -2px;
}
.dtf__sp {
  width: 1px;
  flex: 0 0 auto;
  margin: 6px 0;
  background: var(--line);
}
.dtf__val {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  color: var(--ink);
}
.dtf__val.is-empty {
  color: var(--muted); /* 未选日期 = 占位,灰 */
}
.dtf.is-open {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-tint);
}
.dtf__ic {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.dtf__cv {
  width: 11px;
  height: 11px;
  flex-shrink: 0;
  color: var(--muted);
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform 0.16s;
}
.dtf__date.is-open .dtf__cv,
.dtf__time.is-open .dtf__cv {
  transform: rotate(180deg);
}

/* 时刻浮层:时 / 分两列,贴框的右边 */
.tf__val {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.tf__pop {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  z-index: 30;
  width: 188px;
  padding: 9px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  box-shadow: var(--shadow);
}
.tf__hd {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  margin-bottom: 4px;
}
.tf__hd span {
  text-align: center;
  font-size: 11px;
  color: var(--muted);
}
.tf__cols {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}
.tf__col {
  max-height: 204px;
  overflow-y: auto;
  scrollbar-width: thin;
}
.tf__item {
  width: 100%;
  height: 30px;
  display: grid;
  place-items: center;
  border: 1px solid transparent;
  border-radius: var(--radius-xs);
  background: transparent;
  color: var(--ink);
  font: inherit;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  cursor: pointer;
  transition: background 0.12s;
}
.tf__item:hover {
  background: var(--surface-2);
}
.tf__item:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: -2px;
}
.tf__item.is-sel {
  background: var(--primary);
  border-color: var(--primary);
  color: var(--on-primary);
  font-weight: 600;
}
.tf__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 9px;
  padding-top: 8px;
  border-top: 1px solid var(--line);
}
.dtf__link {
  padding: 2px;
  border: 0;
  background: none;
  color: var(--primary-strong);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
}
.dtf__link:hover {
  text-decoration: underline;
}
.dtf__link:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
}
</style>
