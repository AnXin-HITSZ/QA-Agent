<script setup lang="ts">
defineProps<{ text: string }>();
</script>

<template>
  <div class="uq">
    <div class="uq__body">{{ text }}</div>
  </div>
</template>

<style scoped>
.uq {
  display: flex;
  justify-content: flex-end;
  margin: 14px 0 6px;
}
.uq__body {
  max-width: 72%;
  padding: 10px 15px;
  border-radius: var(--radius) var(--radius) var(--radius-xs) var(--radius);
  background: var(--primary);
  color: var(--on-primary);
  font-size: 15px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  box-shadow: var(--shadow);
}
</style>
