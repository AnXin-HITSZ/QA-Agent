<script setup lang="ts">
import { nextTick, ref, useId, watch } from "vue";
import { searchKnowledgeFiles, type FileSearchResult } from "../api";
import SelectField from "./SelectField.vue";
import FileSearchResults from "./FileSearchResults.vue";
import SearchClearButton from "./SearchClearButton.vue";

const mode = ref<'literal' | 'semantic'>('literal');
const operator = ref<'all' | 'any'>('all');
const query = ref('');
const terms = ref('');
const keywordsInput = ref<HTMLTextAreaElement | null>(null);
const keywordsId = useId();
watch(terms, async () => {
  await nextTick();
  const input = keywordsInput.value;
  if (!input) return;
  input.style.height = '38px';
  input.style.height = Math.max(38, Math.min(116, input.scrollHeight + 2)) + 'px';
});
function clearTerms() {
  terms.value = '';
  keywordsInput.value?.focus({preventScroll:true});
}
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
      <div class="fs-panel__field">
        <label :for="keywordsId">字面关键词</label>
        <div class="fs-panel__search-row">
          <div class="fs-panel__search-box">
            <svg class="fs-panel__search-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5"/><path d="m12.5 12.5 4 4"/></svg>
            <textarea :id="keywordsId" ref="keywordsInput" v-model="terms" required rows="1" :aria-describedby="keywordsId+'-hint'" placeholder="搜索文件正文或名称，例如：聚餐" />
            <SearchClearButton v-if="terms" label="清除字面关键词" @clear="clearTerms" />
          </div>
          <button class="fs-panel__submit" type="submit" :disabled="busy">{{ busy ? '搜索中…' : '搜索文件' }}</button>
        </div>
        <p :id="keywordsId+'-hint'" class="fs-panel__hint">多个词换行输入，连续短语写在同一行；字面结果完整保留。<template v-if="mode==='semantic'">相关发现补充全文与语义结果。</template></p>
      </div>
    </form>
    <p v-if="error" class="fs-panel__error" role="alert">{{ error }}</p>
    <FileSearchResults v-if="result" :initial="result" />
  </section>
</template>
<style scoped>
.fs-panel { padding: 18px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); margin-bottom: 18px; }
.fs-panel__controls { display: flex; gap: 16px; flex-wrap: wrap; }
.fs-panel__field { display: grid; gap: 7px; margin-top: 14px; font-size: 13px; color: var(--muted); }
input, textarea { width: 100%; box-sizing: border-box; border: 1px solid var(--line); border-radius: var(--radius-xs); padding: 8px 12px; background: var(--surface); color: var(--ink); font: inherit; }
input {height:38px;}
input:focus-visible, textarea:focus-visible, button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.fs-panel__search-row {display:flex;align-items:flex-start;gap:10px;}
.fs-panel__search-box {position:relative;flex:1;min-width:0;}
.fs-panel__search-box textarea {display:block;height:38px;min-height:38px;max-height:116px;line-height:20px;padding:8px 42px 8px 34px;resize:none;overflow-y:auto;}
.fs-panel__search-icon {position:absolute;left:11px;top:11px;width:16px;height:16px;stroke:var(--muted);stroke-width:1.5;stroke-linecap:round;pointer-events:none;}
.fs-panel__hint {margin:0;color:var(--muted);font-size:12px;line-height:1.7;}
.fs-panel__submit {height:38px;white-space:nowrap;flex:none;}
button { background: var(--primary); color: var(--on-primary); border: 0; border-radius: var(--radius-xs); padding: 9px 16px; cursor: pointer; font: inherit; }
button:disabled { opacity: .5; cursor: default; } .fs-panel__error { color: var(--seal); font-size: 13px; }
</style>
