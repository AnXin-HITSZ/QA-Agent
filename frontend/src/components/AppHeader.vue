<script setup lang="ts">
import { useTheme } from "../composables/useTheme";

type View = "chat" | "knowledge" | "sops";

defineProps<{ view: View }>();
const emit = defineEmits<{
  (e: "change-view", view: View): void;
}>();

const { theme, toggle } = useTheme();
</script>

<template>
  <header class="hd">
    <div class="hd__lead">
      <div class="hd__mark">
        <span class="hd__name">实验室问答</span>
        <span class="hd__sub">Lab Assistant</span>
      </div>
    </div>

    <div class="hd__right">
      <nav class="hd__tabs" aria-label="视图切换">
        <button
          class="hd__tab"
          type="button"
          :class="{ 'is-active': view === 'chat' }"
          :aria-current="view === 'chat' ? 'page' : undefined"
          @click="emit('change-view', 'chat')"
        >
          对话
        </button>
        <button
          class="hd__tab"
          type="button"
          :class="{ 'is-active': view === 'knowledge' }"
          :aria-current="view === 'knowledge' ? 'page' : undefined"
          @click="emit('change-view', 'knowledge')"
        >
          知识库
        </button>
        <button
          class="hd__tab"
          type="button"
          :class="{ 'is-active': view === 'sops' }"
          :aria-current="view === 'sops' ? 'page' : undefined"
          @click="emit('change-view', 'sops')"
        >
          SOP 流程
        </button>
      </nav>
      <button
        class="hd__theme"
        type="button"
        :aria-label="theme === 'dark' ? '切换到亮色' : '切换到暗色'"
        @click="toggle"
      >
        {{ theme === "dark" ? "☀" : "☾" }}
      </button>
    </div>
  </header>
</template>

<style scoped>
.hd {
  display: flex;
  align-items: flex-end; /* 折页标签坐到页头底线上 */
  justify-content: space-between;
  gap: 16px;
  padding: 14px 24px 0;
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--surface) 72%, var(--paper));
  backdrop-filter: blur(6px);
  position: sticky;
  top: 0;
  z-index: 5;
}
.hd__lead {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 13px; /* 抬离底线,与标签视觉齐平 */
}
.hd__mark {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.hd__name {
  font-family: "Space Grotesk", "IBM Plex Sans", system-ui, sans-serif;
  font-weight: 600;
  font-size: 19px;
  letter-spacing: 0.01em;
  color: var(--ink);
}
.hd__sub {
  font-family: "Space Grotesk", sans-serif;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--muted);
  letter-spacing: 0.04em;
}

.hd__right {
  display: flex;
  align-items: flex-end;
  gap: 14px;
}
.hd__theme {
  flex-shrink: 0;
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  margin-bottom: 6px; /* 抬离底线,与标签视觉齐平 */
  border: 1px solid var(--line);
  border-radius: 50%;
  background: var(--surface);
  color: var(--ink);
  font-size: 15px;
  line-height: 1;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.hd__theme:hover {
  border-color: var(--primary);
  color: var(--primary);
}

/* 折页标签:活动标签白底(与内容区同色)、去下边框、压在底线上,连成一体 */
.hd__tabs {
  display: flex;
  align-items: flex-end;
  gap: 4px;
}
.hd__tab {
  padding: 9px 18px;
  border: 1px solid transparent;
  border-bottom: none;
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  margin-bottom: -1px; /* 压在页头底线上 */
  background: transparent;
  color: var(--muted);
  font: inherit;
  font-size: 14px;
  line-height: 1.4;
  cursor: pointer;
  white-space: nowrap;
  transition: color 0.15s, background 0.15s;
}
.hd__tab:hover {
  color: var(--ink);
}
.hd__tab.is-active {
  color: var(--ink);
  font-weight: 600;
  background: var(--paper); /* 与下方内容区同色 → 连成一体 */
  border-color: var(--line);
  box-shadow: inset 0 2px 0 var(--primary); /* 顶部一抹科研青,标明当前 */
}
.hd__tab:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: -2px;
}
</style>
