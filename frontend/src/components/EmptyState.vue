<script setup lang="ts">
const examples = ["出差高铁票怎么报销?", "办公用品在哪里报销?", "实验室打印机在哪?"];

const emit = defineEmits<{ (e: "pick", text: string): void }>();
</script>

<template>
  <div class="empty">
    <p class="empty__lead">问点什么开始 —— 报销流程或日常答疑都行。</p>
    <div class="empty__examples">
      <button
        v-for="ex in examples"
        :key="ex"
        type="button"
        class="empty__chip"
        @click="emit('pick', ex)"
      >
        {{ ex }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.empty {
  max-width: 560px;
  margin: 52px auto;
  text-align: center;
}
.empty__lead {
  margin: 0 0 20px;
  color: var(--muted);
  font-size: 15px;
}
.empty__examples {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
}
.empty__chip {
  padding: 9px 14px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  font-size: 14px;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s, background 0.15s;
}
.empty__chip:hover {
  border-color: var(--primary);
  color: var(--primary);
  background: var(--primary-tint);
}
</style>
