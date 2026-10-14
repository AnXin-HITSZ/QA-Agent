<script setup lang="ts">
import { ref } from "vue";
import { searchKnowledgeFiles, type FileSearchResult } from "../api";
import SelectField from "./SelectField.vue";
import FileSearchResults from "./FileSearchResults.vue";

const mode = ref<'literal' | 'semantic'>('literal');
const operator = ref<'all' | 'any'>('all');
const query = ref('');
const terms = ref('');
const scope = ref<'all' | 'current'>('all');
const props = defineProps<{ prefix: string }>();
const result = ref<FileSearchResult | null>(null);
const busy = ref(false);
const error = ref('');
const modes = [{ value: 'literal' as const, label: '字面文件搜索' }, { value: 'semantic' as const, label: '相关文件发现' }];
const operators = [{ value: 'all' as const, label: '全部词都出现（AND）' }, { value: 'any' as const, label: '任一词出现（OR）' }];
const scopes = [{ value: 'all' as const, label: '全部分类' }, { value: 'current' as const, label: '当前分类及子分类' }];
async function run() {
  busy.value = true; error.value = ''; result.value = null;
  try { result.value = await searchKnowledgeFiles({ mode: mode.value, query: query.value,
    literal_terms: terms.value.split('\n').filter(t => t.length > 0), literal_operator: operator.value,
    prefix: scope.value === 'current' ? props.prefix : '', fields: ['name', 'body'], page_size: 20 }); }
  catch (e) { error.value = e instanceof Error ? e.message : String(e); }
  finally { busy.value = false; }
}
</script>
<template>
  <section class="fs-panel">
    <form @submit.prevent="run">
      <div class="fs-panel__controls">
        <SelectField v-model="mode" :options="modes" label="搜索方式" />
        <SelectField v-model="operator" :options="operators" label="字面条件" />
        <SelectField v-model="scope" :options="scopes" label="范围" />
      </div>
      <label v-if="mode === 'semantic'" class="fs-panel__field">查询主题<input v-model="query" required maxlength="500" placeholder="例如：发票报销" /></label>
      <label class="fs-panel__field">字面关键词<textarea v-model="terms" required rows="2" placeholder="每行一个词；连续短语写在同一行，例如：聚餐" /></label>
      <div class="fs-panel__foot"><p>字面结果完整保留；相关文件发现补充全文与语义结果。</p><button type="submit" :disabled="busy">{{ busy ? '搜索中…' : '搜索文件' }}</button></div>
    </form>
    <p v-if="error" class="fs-panel__error" role="alert">{{ error }}</p>
    <FileSearchResults v-if="result" :initial="result" />
  </section>
</template>
<style scoped>
.fs-panel { padding: 18px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); margin-bottom: 18px; }
.fs-panel__controls { display: flex; gap: 16px; flex-wrap: wrap; }
.fs-panel__field { display: grid; gap: 7px; margin-top: 14px; font-size: 13px; color: var(--muted); }
input, textarea { width: 100%; box-sizing: border-box; border: 1px solid var(--line); border-radius: var(--radius-xs); padding: 10px 12px; background: var(--surface); color: var(--ink); font: inherit; resize: vertical; }
input:focus-visible, textarea:focus-visible, button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.fs-panel__foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-top: 12px; }
.fs-panel__foot p { font-size: 12px; color: var(--muted); margin: 0; }
button { background: var(--primary); color: var(--on-primary); border: 0; border-radius: var(--radius-xs); padding: 9px 16px; cursor: pointer; font: inherit; }
button:disabled { opacity: .5; cursor: default; } .fs-panel__error { color: var(--seal); font-size: 13px; }
</style>
