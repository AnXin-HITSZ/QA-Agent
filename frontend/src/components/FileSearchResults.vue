<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { fileSearchPage, exportFileSearch, downloadKnowledgeFile, type FileSearchResult } from "../api";

const props = defineProps<{ initial: FileSearchResult }>();
const result = ref({ ...props.initial, items: [] } as FileSearchResult);
const busy = ref(false);
const error = ref("");
watch(() => props.initial, value => { result.value = { ...value, items: [] }; void turn(value.offset); });
onMounted(() => { void turn(props.initial.offset); });
const literal = computed(() => result.value.items.filter(i => i.routes.includes("literal")));
const supplement = computed(() => result.value.items.filter(i => !i.routes.includes("literal")));
const labels: Record<string, string> = { literal: "字面匹配", fulltext: "全文相关", vector: "语义相关" };

let requestSeq = 0;
async function turn(offset: number) {
  const seq = ++requestSeq;
  busy.value = true; error.value = "";
  try { const next = await fileSearchPage(result.value.search_id, offset, result.value.page_size); if (seq === requestSeq) result.value = next; }
  catch (e) { if (seq === requestSeq) error.value = e instanceof Error ? e.message : String(e); }
  finally { if (seq === requestSeq) busy.value = false; }
}
async function action(fn: () => Promise<void>) {
  busy.value = true; error.value = "";
  try { await fn(); } catch (e) { error.value = e instanceof Error ? e.message : String(e); }
  finally { busy.value = false; }
}
</script>

<template>
  <section class="fs-results" aria-label="文件搜索结果">
    <header class="fs-results__head">
      <div>
        <h3>文件搜索结果</h3>
        <p>字面匹配 {{ result.literal_total }} 个 · 相关性补充 {{ result.supplement_total }} 个</p>
      </div>
      <button type="button" :disabled="busy" @click="action(() => exportFileSearch(result.search_id))">导出清单</button>
    </header>
    <p class="fs-results__hint">{{ result.literal_terms.join(result.literal_operator === 'all' ? ' AND ' : ' OR ') }} · {{ result.prefix || '全部分类' }}</p>
    <p class="fs-results__hint">正文可搜索 {{ result.coverage.body_indexed }}/{{ result.coverage.source_files }} 个；{{ result.coverage.body_uncovered }} 个正文尚未覆盖或版本已变化。{{ result.mode === 'semantic' ? '相关性补充不保证穷尽。' : '' }}</p>
    <p v-for="note in result.notes" :key="note" class="fs-results__hint">{{ note }}</p>
    <p v-if="error" role="alert" class="fs-results__error">{{ error }}</p>
    <p v-if="result.unavailable_on_page" class="fs-results__hint">本页 {{ result.unavailable_on_page }} 个原件已删除或更新，未展示旧内容。</p>
    <template v-for="group in [{ title: '字面匹配', items: literal }, { title: '相关性补充', items: supplement }]" :key="group.title">
      <section v-if="group.items.length" class="fs-results__group" :aria-label="group.title">
        <header class="fs-results__group-head"><h4>{{ group.title }}</h4><span class="fs-results__count">本页 {{ group.items.length }} 个文件</span><span class="fs-results__group-note">点击文件概要查看匹配内容</span></header>
        <ul class="fs-results__list">
          <li v-for="item in group.items" :key="`${result.search_id}:${result.offset}:${item.key}`">
            <details class="fs-results__details">
              <summary>
                <svg class="fs-results__chevron" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m6 3 5 5-5 5" /></svg>
                <div class="fs-results__overview">
                  <strong>{{ item.name }}</strong>
                  <p class="fs-results__path">{{ item.key }}</p>
                  <div class="fs-results__meta"><span v-for="route in item.routes" :key="route" class="fs-results__route">{{ labels[route] || route }}</span><span v-if="item.matched_terms.length" class="fs-results__terms">命中关键词：{{ item.matched_terms.join('、') }}</span><span class="fs-results__expand"><span class="when-closed">查看匹配内容</span><span class="when-open">收起匹配内容</span></span></div>
                </div>
              </summary>
              <div class="fs-results__detail-body">
                <h5>匹配内容</h5>
                <p v-for="(e, i) in item.evidence" :key="i" class="fs-results__evidence"><span v-if="e.page" class="fs-results__location">第 {{ e.page }} 页 · </span><span v-if="e.section.length" class="fs-results__location">{{ e.section.join(' › ') }} · </span>{{ e.text }}</p>
                <p v-if="!item.evidence.length" class="fs-results__hint">暂无可展示的正文片段，可下载原文件查看。</p>
              </div>
            </details>
            <button class="fs-results__download" type="button" :disabled="busy" :aria-label="`下载该文件：${item.name}`" @click="action(() => downloadKnowledgeFile(item.key))">下载</button>
          </li>
        </ul>
      </section>
    </template>
    <p v-if="!result.total" class="fs-results__hint">当前搜索范围内没有匹配文件。</p>
    <nav class="fs-results__pages" aria-label="搜索结果分页">
      <button type="button" :disabled="busy || result.offset === 0" @click="turn(Math.max(0, result.offset - result.page_size))">上一页</button>
      <span>共 {{ result.total }} 个 · 当前从第 {{ result.total ? result.offset + 1 : 0 }} 个开始</span>
      <button type="button" :disabled="busy || result.next_offset === null" @click="turn(result.next_offset!)">下一页</button>
    </nav>
  </section>
</template>

<style scoped>
.fs-results { margin-top: 18px; padding: 18px; border: 1px solid var(--line); border-radius: var(--radius-sm); background: var(--surface); color: var(--ink); }
.fs-results__head, .fs-results__pages { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
h3, h4, h5, p { margin: 0; } h3 { font-size: 16px; } h4 { font-size: 14px; }
.fs-results__head p, .fs-results__hint, .fs-results__path { color: var(--muted); font-size: 12px; margin-top: 7px; overflow-wrap: anywhere; }
button { border: 1px solid var(--line); border-radius: var(--radius-xs); padding: 6px 12px; background: var(--surface); color: var(--primary); cursor: pointer; font: inherit; font-size: 12px; }
button:hover:not(:disabled) { background: var(--primary-tint); } button:disabled { opacity: .5; cursor: default; } button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.fs-results__group {margin-top:18px;border:1px solid var(--line);border-radius:var(--radius-sm);overflow:hidden;}
.fs-results__group-head {display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 14px;background:var(--primary-tint);border-bottom:1px solid var(--line);color:var(--primary-strong);}
.fs-results__count {font-size:11px;padding:2px 7px;border-radius:99px;background:var(--surface);color:var(--primary-strong);}
.fs-results__group-note {margin-left:auto;font-size:12px;color:var(--muted);}
.fs-results__list {list-style:none;padding:0;margin:0;}
.fs-results__list li {position:relative;border-bottom:1px solid var(--line);}
.fs-results__list li:last-child {border-bottom:0;}
.fs-results__details summary {display:flex;align-items:flex-start;gap:10px;padding:16px 76px 16px 14px;cursor:pointer;list-style:none;transition:background-color .12s;}
.fs-results__details summary::-webkit-details-marker {display:none;}
.fs-results__details summary:hover {background:var(--surface-2);}
.fs-results__details summary:focus-visible {outline:2px solid var(--focus);outline-offset:-2px;background:var(--primary-tint);}
.fs-results__chevron {width:14px;height:14px;margin-top:3px;flex-shrink:0;stroke:var(--muted);stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round;transition:transform .15s;}
.fs-results__details[open] .fs-results__chevron {transform:rotate(90deg);}
.fs-results__overview {min-width:0;flex:1;}
.fs-results__overview strong {font-size:13px;font-weight:600;line-height:1.6;overflow-wrap:anywhere;}
.fs-results__meta {display:flex;align-items:center;gap:6px 10px;flex-wrap:wrap;margin-top:8px;font-size:11px;}
.fs-results__route {padding:1px 6px;border-radius:99px;background:var(--primary-tint);color:var(--primary-strong);}
.fs-results__terms {color:var(--muted);overflow-wrap:anywhere;}
.fs-results__expand {color:var(--primary);}
.when-open,.fs-results__details[open] .when-closed {display:none;}
.fs-results__details[open] .when-open {display:inline;}
.fs-results__detail-body {padding:14px 16px 16px;margin:0 14px 14px 38px;border:1px solid var(--line);border-radius:var(--radius-xs);background:var(--surface-2);}
.fs-results__detail-body h5 {font-size:12px;font-weight:600;color:var(--muted);}
.fs-results__evidence {font-size:13px;margin-top:10px;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7;}
.fs-results__evidence+.fs-results__evidence {border-top:1px solid var(--line);padding-top:10px;}
.fs-results__location {color:var(--muted);font-size:12px;}
.fs-results__download {position:absolute;right:14px;top:17px;height:24px;padding:0 8px;border:0;border-radius:var(--radius-xs);background:transparent;color:var(--muted);font:inherit;font-size:12px;cursor:pointer;transition:color .12s,background .12s;}
.fs-results__download:hover:not(:disabled),.fs-results__download:focus-visible {color:var(--primary);background:var(--primary-tint);}
@media(max-width:640px) {.fs-results {padding:12px;}.fs-results__group-note {margin-left:0;flex-basis:100%;}.fs-results__detail-body {margin-left:14px;}}
@media(prefers-reduced-motion:reduce) {.fs-results__chevron,.fs-results__details summary,.fs-results__download {transition:none;}}
.fs-results__pages { margin-top: 16px; font-size: 12px; color: var(--muted); } .fs-results__error { color: var(--seal); margin-top: 10px; }
</style>
