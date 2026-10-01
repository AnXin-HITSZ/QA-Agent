<script setup lang="ts">
import { computed, onMounted } from "vue";

import type { Source, SopImageReference } from "../api";
import type { Step, ToolActivity } from "../composables/useChat";
import { useSops } from "../composables/useSops";
import MarkdownView from "./MarkdownView.vue";
import SopImageGallery from "./SopImageGallery.vue";

const props = defineProps<{
  steps?: Step[];
  skill?: string | null;
  sources?: Source[];
  images?: SopImageReference[];
  streaming?: boolean;
  stopped?: boolean;
}>();

const list = computed<Step[]>(() => props.steps ?? []);
const hasContent = computed(() => list.value.some((s) => s.text || s.tools.length));
const sources = computed<Source[]>(() => props.sources ?? []);

// 展示名:优先文件名,退而取 oss_key 的末段。
function srcName(s: Source): string {
  if (s.source) return s.source;
  const base = (s.oss_key || "").split("/").filter(Boolean).pop();
  return base || s.oss_key || "未知来源";
}

// skill id → 中文名:SOP 名字是用户自己写的(可在 SOP 视图随时新建 / 改名),
// 这里一律以列表为准;列表还没到位(或没接通)才退回原样显示 id。
const { list: sops, loaded: sopsLoaded, loadList: loadSops } = useSops();
onMounted(() => {
  if (!sopsLoaded.value) void loadSops(); // 没逛过 SOP 视图也要有名字可查
});

function label(id: string): string {
  return sops.value.find((s) => s.id === id)?.name ?? id;
}

// 工具活动 → 人类可读的一行说明(意图,不暴露原始工具名的技术细节)。
function actLabel(a: ToolActivity): string {
  if (a.name === "read_sop_image") return "查看 SOP 图片";
  if (a.name === "list_sops") {
    return "查看 SOP 目录";
  }
  if (a.name === "get_sop") {
    const id = typeof a.args?.skill_id === "string" ? a.args.skill_id : "";
    return id ? `读取「${label(id)}」` : "读取 SOP";
  }
  if (a.name === "search_knowledge") return "检索知识库";
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
      <p v-else-if="stopped" class="ar__stopped">已停止生成</p>
    </div>

    <SopImageGallery :images="images ?? []" />
    <footer v-if="sources.length" class="ar__sources">
      <p class="ar__srchead">参考来源</p>
      <ul class="ar__srclist">
        <li v-for="s in sources" :key="s.oss_key" class="src">
          <a
            v-if="s.url"
            class="src__link"
            :href="s.url"
            target="_blank"
            rel="noopener"
          >
            <span class="src__doc" aria-hidden="true">📄</span>
            <span class="src__name">{{ srcName(s) }}</span>
          </a>
          <span v-else class="src__link src__link--off" title="下载链接暂不可用">
            <span class="src__doc" aria-hidden="true">📄</span>
            <span class="src__name">{{ srcName(s) }}</span>
            <span class="src__off">链接不可用</span>
          </span>
          <span v-if="s.category" class="src__cat">{{ s.category }}</span>
          <span v-if="s.score != null" class="src__score">{{ s.score.toFixed(2) }}</span>
        </li>
      </ul>
    </footer>
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
/* 用户自己按了停止:留一句安静的说明,免得半截答案被当成完整回答 */
.ar__stopped {
  margin: 8px 0 2px;
  padding-left: 10px;
  border-left: 2px solid var(--line);
  color: var(--muted);
  font-size: 12.5px;
}

/* 参考来源:答案末尾的常驻清单,与正文以发丝线分隔,安静不抢戏 */
.ar__sources {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--line);
}
.ar__srchead {
  margin: 0 0 6px;
  font-family: "IBM Plex Sans", "Noto Sans SC", sans-serif;
  font-size: 12px;
  letter-spacing: 0.02em;
  color: var(--muted);
}
.ar__srclist {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.src {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
}
.src__link {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  color: var(--primary);
  text-decoration: none;
  border-bottom: 1px solid transparent;
}
.src__link:hover {
  border-bottom-color: currentColor;
}
.src__link--off {
  color: var(--muted);
  cursor: default;
}
.src__doc {
  font-size: 12px;
}
.src__name {
  word-break: break-all;
}
.src__off {
  font-size: 11.5px;
}
.src__cat {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11px;
  color: var(--muted);
}
.src__score {
  margin-left: auto;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11px;
  color: var(--muted);
  opacity: 0.6;
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
