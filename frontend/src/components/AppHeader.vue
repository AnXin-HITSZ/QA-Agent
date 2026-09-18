<script setup lang="ts">
import { useTheme } from "../composables/useTheme";

const { theme, toggle } = useTheme();
</script>

<template>
  <header class="hd">
    <div class="hd__mark">
      <span class="hd__name">实验室问答</span>
      <span class="hd__sub">Lab Assistant</span>
    </div>
    <button
      class="hd__theme"
      type="button"
      :aria-label="theme === 'dark' ? '切换到亮色' : '切换到暗色'"
      @click="toggle"
    >
      {{ theme === "dark" ? "☀" : "☾" }}
    </button>
  </header>
</template>

<style scoped>
.hd {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 24px;
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--surface) 72%, var(--paper));
  backdrop-filter: blur(6px);
  position: sticky;
  top: 0;
  z-index: 5;
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
.hd__theme {
  flex-shrink: 0;
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
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
</style>
