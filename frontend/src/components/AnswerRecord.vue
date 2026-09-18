<script setup lang="ts">
import { computed } from "vue";

import type { Step, ToolActivity } from "../composables/useChat";
import MarkdownView from "./MarkdownView.vue";

const props = defineProps<{
  steps?: Step[];
  skill?: string | null;
  streaming?: boolean;
}>();

const list = computed<Step[]>(() => props.steps ?? []);
const hasContent = computed(() => list.value.some((s) => s.text || s.tools.length));

// skill id → 中文名。未收录的 id 原样显示。
const SKILL_NAMES: Record<string, string> = {
  "travel-reimbursement": "差旅报销",
  "supplies-reimbursement": "办公用品报销",
};

function label(id: string): string {
  return SKILL_NAMES[id] ?? id;
}

// 工具活动 → 人类可读的一行说明(意图,不暴露原始工具名的技术细节)。
function actLabel(a: ToolActivity): string {
  if (a.name === "search_sops") {
    const q = typeof a.args?.query === "string" ? a.args.query : "";
    return q ? `检索 SOP:${q}` : "检索全部 SOP";
  }
  if (a.name === "get_sop") {
    const id = typeof a.args?.skill_id === "string" ? a.args.skill_id : "";
    return id ? `读取「${label(id)}」` : "读取 SOP";
  }
  return a.name;
}
</script>

<template>
  <article class="ar" :class="{ 'ar--sop': skill }">
    <header v-if="skill" class="ar__match">
      <span class="ar__seal" aria-hidden="true">◉</span>
      <span class="ar__label">{{ label(skill) }}指引</span>
      <span class="ar__id">{{ skill }}</span>
    </header>

    <div class="ar__timeline">
      <template v-for="(s, i) in list" :key="i">
        <!-- 思考段:该轮以调用工具收尾 → 弱化灰字 + 可折叠 + 工具行 -->
        <details v-if="s.tools.length" class="think" open>
  <summary class="think__sum">
<span class="think__chev" aria-hidden="true" />
            <span class="think__cap">思考过程</span>
   </summary>
          <MarkdownView v-if="s.text" :source="s.text" class="think__md" />
          <ul class="think__tools">
            <li
      v-for="(a, j) in s.tools"
  :key="j"
     class="tool"
         :class="{ 'is-done': a.done }"
            >
    <span class="tool__dot" aria-hidden="true" />
         <span class="tool__txt">{{ actLabel(a) }}</span>
        <span v-if="a.done" class="tool__ok" aria-hidden="true">✓</span>
      </li>
     </ul>
        </details>

        <!-- 最终答案(或流式中尚未调用工具的收尾段):正常文档样式 -->
        <MarkdownView v-else-if="s.text" :source="s.text" />
      </template>

      <p v-if="streaming && !hasContent" class="ar__pending" aria-live="polite">正在整理答案…</p>
    </div>
  </article>
</template>

<style scoped>
.ar {
  margin: 6px 0 8px;
  padding: 6px 22px 14px;
  border: 1px solid var(--line);
  border-left: 3px solid var(--line);
  border-radius: var(--radius-xs) var(--radius) var(--radius) var(--radius-xs);
  background: var(--surface);
  box-shadow: var(--shadow);
  animation: ar-in 0.34s cubic-bezier(0.22, 1, 0.36, 1) both;
}
.ar--sop {
  border-left-color: var(--primary);
}
.ar__match {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 8px;
  padding: 12px 0 8px;
  border-bottom: 1px solid var(--line);
}
.ar__seal {
  color: var(--seal);
  font-size: 15px;
}
.ar__label {
  font-family: "Space Grotesk", "IBM Plex Sans", "Noto Sans SC", sans-serif;
  font-weight: 600;
  font-size: 14.5px;
  color: var(--ink);
}
.ar__id {
  margin-left: auto;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11.5px;
  color: var(--muted);
}

.ar__timeline {
  display: flex;
  flex-direction: column;
}

/* 思考段:虚线左脊 + 弱化,读作「过程旁注」,与答案的实心色脊区分 */
.think {
  margin: 6px 0;
  padding: 2px 0 4px 14px;
  border-left: 2px dashed var(--line);
}
.think__sum {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 2px 0;
cursor: pointer;
  list-style: none;
  color: var(--muted);
  font-size: 12.5px;
  user-select: none;
}
.think__sum::-webkit-details-marker {
  display: none;
}
.think__cap {
  font-family: "IBM Plex Sans", "Noto Sans SC", sans-serif;
letter-spacing: 0.02em;
}
.think__chev {
  width: 6px;
height: 6px;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  transform: rotate(-45deg);
  transition: transform 0.18s ease;
}
.think[open] .think__chev {
  transform: rotate(45deg);
}
.think__md {
  margin-top: 4px;
  opacity: 0.6;
  font-size: 0.95em;
}
.think__tools {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 8px 0 2px;
  padding: 0;
  list-style: none;
}
.tool {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  align-self: flex-start;
  max-width: 100%;
  padding: 5px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: color-mix(in srgb, var(--primary) 5%, transparent);
  font-size: 12.5px;
}
.tool__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--primary);
  animation: act-pulse 1.1s infinite ease-in-out;
}
.tool__txt {
  color: var(--ink);
}
.tool.is-done .tool__dot {
  animation: none;
  opacity: 1;
}
.tool__ok {
  margin-left: 2px;
  color: var(--primary);
  font-weight: 700;
}
.ar__pending {
  margin: 8px 0 2px;
  color: var(--muted);
  font-size: 13.5px;
}

@keyframes act-pulse {
  0%,
  100% {
    opacity: 0.3;
  }
  50% {
    opacity: 1;
  }
}

@keyframes ar-in {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
@media (prefers-reduced-motion: reduce) {
  .ar {
    animation: none;
  }
  .tool__dot {
    animation: none;
    opacity: 0.6;
  }
  .think__chev {
    transition: none;
  }
}
</style>
