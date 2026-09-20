<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ prefix: string }>();
const emit = defineEmits<{ (e: "navigate", prefix: string): void }>();

// 把 "报销/差旅/" 拆成可逐级跳转的面包屑(每级带其累积前缀)。
const crumbs = computed(() => {
  const parts = props.prefix.split("/").filter(Boolean);
  let acc = "";
  return parts.map((name) => {
    acc += name + "/";
    return { name, prefix: acc };
  });
});
</script>

<template>
  <nav class="bc" aria-label="当前位置">
    <button
      class="bc__seg"
      type="button"
      :class="{ 'is-current': !crumbs.length }"
      :disabled="!crumbs.length"
      @click="emit('navigate', '')"
    >
      知识库
    </button>
    <template v-for="(c, i) in crumbs" :key="c.prefix">
      <span class="bc__sep" aria-hidden="true">/</span>
      <button
        class="bc__seg"
        type="button"
        :class="{ 'is-current': i === crumbs.length - 1 }"
        :disabled="i === crumbs.length - 1"
        @click="emit('navigate', c.prefix)"
      >
        {{ c.name }}
      </button>
    </template>
  </nav>
</template>

<style scoped>
.bc {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 2px;
  min-width: 0;
}
.bc__seg {
  padding: 3px 6px;
  border: 0;
  border-radius: var(--radius-xs);
  background: transparent;
  color: var(--primary);
  font: inherit;
  font-size: 14px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.bc__seg:hover:not(:disabled) {
  background: var(--primary-tint);
}
.bc__seg.is-current {
  color: var(--ink);
  font-weight: 600;
  cursor: default;
}
.bc__sep {
  color: var(--muted);
  font-size: 13px;
  user-select: none;
}
</style>
