<script setup lang="ts" generic="T extends string">
import { computed, nextTick, onBeforeUnmount, ref, useId } from "vue";

// 通用下拉选择:自绘触发按钮 + 浮层列表。
// 为什么不直接用原生 <select>:控件本身还能凑合,但**展开的列表由系统绘制**
// (Windows 上是系统配色 + 系统字体),拿不到本项目的令牌与圆角,在页面里很突兀。
//
// 只做「单选 + 值变了通知」这一件事,不认识业务语义;选项文案全部由调用方给。
// 键盘:Enter / Space / ↓ / ↑ 展开,↑↓ 移动,Home / End 到首尾,Enter 选中,
// Esc 关闭(焦点留在触发按钮上),Tab 关闭并移走。
const props = defineProps<{
  modelValue: T;
  options: { value: T; label: string }[];
  label?: string; // 左侧小标签(与页面其它控件同一排版)
}>();
const emit = defineEmits<{ "update:modelValue": [v: T]; change: [v: T] }>();

// aria 关联用的 id:同页可能挂多个下拉,不能撞(useId 由 Vue 保证唯一)
const uid = useId();
const labelId = `${uid}-label`;
const valueId = `${uid}-value`;
const listId = `${uid}-list`;
const optId = (i: number): string => `${uid}-opt-${i}`;

const open = ref(false);
const active = ref(0); // 键盘高亮 / 鼠标悬停项
const alignRight = ref(false); // 靠视口右侧时浮层改贴右边缘,免得被切掉
const root = ref<HTMLElement | null>(null);
const body = ref<HTMLElement | null>(null); // 浮层的定位参照(按钮 + 列表)
const list = ref<HTMLElement | null>(null);
let swallowClick = false; // 键盘选完后可能跟来的合成 click,吞掉一次(见 onKeydown / onClick)

const current = computed(() => props.options.find((o) => o.value === props.modelValue));
const labelledBy = computed(() => (props.label ? `${labelId} ${valueId}` : undefined));

function openList(): void {
  if (open.value) return;
  open.value = true;
  active.value = Math.max(0, props.options.findIndex((o) => o.value === props.modelValue));
  document.addEventListener("pointerdown", onOutside, true);
  void nextTick(() => {
    place();
    scrollActiveIntoView();
  });
}

function close(): void {
  if (!open.value) return;
  open.value = false;
  document.removeEventListener("pointerdown", onOutside, true);
}

// 点组件之外任何地方都收起(捕获阶段监听,页面上其它控件先收到点击也不影响收起)
function onOutside(e: Event): void {
  if (!root.value?.contains(e.target as Node)) close();
}

function toggle(): void {
  if (open.value) close();
  else openList();
}

// 触发按钮上的点击:展开 / 收起。键盘按 Enter / Space 时浏览器也会补一个合成 click,
// 这里要把它和真实鼠标点击分开 —— 见 onKeydown 的 closed 分支与 swallowClick。
function onClick(): void {
  if (swallowClick) {
    swallowClick = false;
    return;
  }
  toggle();
}

// 真实的鼠标 / 触摸交互一定先有 pointerdown,此时前面留下的标记一律作废。
function onPointerDown(): void {
  swallowClick = false;
}

// 浮层会不会顶出视口右侧:会则贴右边缘(取展开后的实际宽度,故放在 nextTick 里量)
function place(): void {
  const r = body.value?.getBoundingClientRect();
  const w = list.value?.offsetWidth ?? 0;
  alignRight.value = Boolean(r) && r!.left + w > window.innerWidth - 8;
}

function scrollActiveIntoView(): void {
  list.value?.querySelector<HTMLElement>(".sf__opt.is-active")?.scrollIntoView({ block: "nearest" });
}

function move(delta: number): void {
  const n = props.options.length;
  if (!n) return;
  active.value = (active.value + delta + n) % n;
  scrollActiveIntoView();
}

function pick(option: { value: T; label: string }): void {
  const changed = option.value !== props.modelValue;
  close();
  if (!changed) return; // 与原生 <select> 一致:选回原值不触发 change(不白跑一次请求)
  emit("update:modelValue", option.value);
  emit("change", option.value);
}

function onKeydown(e: KeyboardEvent): void {
  if (!open.value) {
    // 收起时只有方向键由这里展开;Enter / Space 交给浏览器的原生 click(再走 toggle),
    // 免得同一次按键既开又关。
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      openList();
    }
    return;
  }
  switch (e.key) {
    case "ArrowDown":
      e.preventDefault();
      move(1);
      break;
    case "ArrowUp":
      e.preventDefault();
      move(-1);
      break;
    case "Home":
      e.preventDefault();
      active.value = 0;
      scrollActiveIntoView();
      break;
    case "End":
      e.preventDefault();
      active.value = props.options.length - 1;
      scrollActiveIntoView();
      break;
    case "Enter":
    case " ":
      e.preventDefault(); // 别顺手提交外层表单;也尽量压掉随后的合成 click
      swallowClick = true; // 万一浏览器仍补了 click,由 onClick 认领掉(不然会又展开一次)
      pick(props.options[active.value]);
      break;
    case "Escape":
      e.preventDefault();
      e.stopPropagation();
      close();
      break;
    case "Tab":
      close();
      break;
  }
}

onBeforeUnmount(() => document.removeEventListener("pointerdown", onOutside, true));
</script>

<template>
  <div ref="root" class="sf" :class="{ 'sf--open': open, 'sf--right': alignRight }" @pointerdown="onPointerDown">
    <span v-if="label" :id="labelId" class="sf__label">{{ label }}</span>
    <!-- 浮层的定位参照是按钮本身(不是整个「标签 + 按钮」),列表才会从控件左边缘长出来 -->
    <div ref="body" class="sf__body">
      <button
        class="sf__btn"
        type="button"
        aria-haspopup="listbox"
        :aria-expanded="open"
        :aria-controls="listId"
        :aria-labelledby="labelledBy"
        :aria-activedescendant="open ? optId(active) : undefined"
        @click="onClick"
        @keydown="onKeydown"
      >
        <span :id="valueId" class="sf__value">{{ current?.label ?? "" }}</span>
        <svg class="sf__cv" viewBox="0 0 12 12" aria-hidden="true">
          <polyline points="2.5,4.5 6,8 9.5,4.5" />
        </svg>
      </button>

      <ul v-if="open" :id="listId" ref="list" class="sf__list" role="listbox" :aria-labelledby="labelId">
        <li
          v-for="(o, i) in options"
          :id="optId(i)"
          :key="o.value"
          class="sf__opt"
          :class="{ 'is-sel': o.value === modelValue, 'is-active': i === active }"
          role="option"
          :aria-selected="o.value === modelValue"
          @click="pick(o)"
          @mousemove="active = i"
        >
          <span class="sf__optT">{{ o.label }}</span>
          <svg v-if="o.value === modelValue" class="sf__check" viewBox="0 0 14 14" aria-hidden="true">
            <polyline points="2.5,7.5 5.5,10.5 11.5,3.5" />
          </svg>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.sf {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: var(--muted);
}
.sf__body {
  position: relative;
  min-width: 0;
}
.sf__btn {
  height: 38px;
  min-width: 150px;
  max-width: 260px;
  padding: 0 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  font-size: 13.5px;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.sf__btn:hover {
  border-color: var(--primary);
}
.sf__btn:focus-visible {
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-tint);
}
.sf--open .sf__btn {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-tint);
}
.sf__value {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.sf__cv {
  width: 11px;
  height: 11px;
  flex: 0 0 auto;
  color: var(--muted);
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform 0.16s, color 0.16s;
}
.sf--open .sf__cv {
  transform: rotate(180deg);
  color: var(--primary-strong);
}

/* 浮层列表:自绘(原生列表由系统绘制,令牌用不上) */
.sf__list {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  z-index: 30;
  min-width: 100%;
  max-width: 340px;
  max-height: 264px;
  overflow-y: auto;
  margin: 0;
  padding: 4px;
  list-style: none;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  box-shadow: var(--shadow);
}
.sf--right .sf__list {
  left: auto;
  right: 0;
}
.sf__opt {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  height: 32px;
  padding: 0 8px 0 10px;
  border-radius: var(--radius-xs);
  color: var(--ink);
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
}
.sf__opt.is-active {
  background: var(--surface-2);
}
.sf__opt.is-sel {
  color: var(--primary-strong);
  font-weight: 600;
}
.sf__opt.is-sel.is-active {
  background: var(--primary-tint);
}
.sf__optT {
  overflow: hidden;
  text-overflow: ellipsis;
}
.sf__check {
  width: 13px;
  height: 13px;
  flex: 0 0 auto;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.9;
  stroke-linecap: round;
  stroke-linejoin: round;
}

/* 窄屏:标签挪到控件上方,和页面其它控件一致 */
@media (max-width: 540px) {
  .sf {
    flex: 1 1 42%;
    flex-direction: column;
    align-items: stretch;
    gap: 5px;
  }
  .sf__btn {
    width: 100%;
    min-width: 0;
    max-width: none;
  }
}
</style>
