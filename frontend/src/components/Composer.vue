<script setup lang="ts">
import { nextTick, ref } from "vue";

const props = defineProps<{ loading: boolean }>();
const emit = defineEmits<{ (e: "send", text: string): void }>();

const input = ref("");
const ta = ref<HTMLTextAreaElement | null>(null);

function autogrow(): void {
  const el = ta.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
}

function submit(): void {
  const text = input.value.trim();
  if (!text || props.loading) return;
  emit("send", text);
  input.value = "";
  nextTick(autogrow);
}
</script>

<template>
  <form class="cp" @submit.prevent="submit">
    <textarea
      ref="ta"
      v-model="input"
      class="cp__input"
      rows="1"
      placeholder="输入问题,Enter 发送,Shift+Enter 换行"
      @input="autogrow"
      @keydown.enter.exact.prevent="submit"
    />
    <button class="cp__send" type="submit" :disabled="loading || !input.trim()">发送</button>
  </form>
</template>

<style scoped>
.cp {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  padding: 8px 8px 8px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow);
  transition: border-color 0.15s;
}
.cp:focus-within {
  border-color: var(--primary);
}
.cp__input {
  flex: 1;
  max-height: 160px;
  padding: 6px 0;
  border: none;
  background: transparent;
  color: var(--ink);
  font: inherit;
  font-size: 15px;
  line-height: 1.5;
  resize: none;
  outline: none;
}
.cp__input::placeholder {
  color: var(--muted);
}
.cp__send {
  flex-shrink: 0;
  padding: 9px 18px;
  border: none;
  border-radius: var(--radius-sm);
  background: var(--primary);
  color: var(--on-primary);
  font: inherit;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s;
}
.cp__send:hover:not(:disabled) {
  background: var(--primary-strong);
}
.cp__send:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
</style>
