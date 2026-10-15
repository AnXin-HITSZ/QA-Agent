<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { getIndexJob, getIndexJobFiles, type IndexJobStatus, type IndexJobFileRow } from "../api";
import { modeLabel } from "../composables/useKnowledge";
import { errorText, formatStampFull } from "../lib/format";
import SideDrawer from "./SideDrawer.vue";

const props = defineProps<{ job: IndexJobStatus; canRetry?: boolean }>();
const emit = defineEmits<{ close: []; retry: [row: IndexJobFileRow] }>();
const current = ref(props.job);
const files = ref<IndexJobFileRow[]>([]);
const offset = ref(0);
const total = ref(0);
const loading = ref(false);
const error = ref("");
const size = 20;
let sequence = 0;
const statuses: Record<string,string> = {queued:"排队中",running:"进行中",published:"已发布",failed:"未发布"};
const scopeText = computed(() => current.value.scope.kind === "keys"
  ? `${current.value.scope.keys?.length ?? 0} 个选定文件` : current.value.scope.prefix || "整个知识库");
const pageStats = computed(() => current.value.summary?.pages);
const summary = computed(() => current.value.summary);
async function load(start = 0) {
  const request = ++sequence;
  const id = props.job.job_id;
  loading.value = true;error.value = "";
  files.value = [];
  const [status, page] = await Promise.allSettled([getIndexJob(id), getIndexJobFiles(id,start,size)]);
  if (request !== sequence) return;
  if (status.status === "fulfilled") current.value = status.value;
  else error.value = `任务状态读取失败：${errorText(status.reason)}`;
  if (page.status === "fulfilled") {files.value=page.value.items;total.value=page.value.total;offset.value=start;}
  else error.value += `${error.value ? '；' : ''}文件明细读取失败：${errorText(page.reason)}`;
  loading.value = false;
}
watch(() => props.job, value => {current.value = value;});
watch(() => props.job.job_id, () => {total.value=0;offset.value=0;void load();}, {immediate:true});
onBeforeUnmount(() => {sequence++;});
function fileStatus(row: IndexJobFileRow) {
  if (row.status === "indexed") return current.value.published ? `已入库 ${row.chunks} 块` : `候选写入 ${row.chunks} 块`;
  return row.status === "failed" ? "失败" : "跳过";
}
</script>

<template>
  <SideDrawer title="索引任务详情" :subtitle="current.job_id" @close="emit('close')">
    <template #badge><span class="job-tag" :class="{'job-tag--bad':current.status==='failed'}">{{ statuses[current.status] }}</span></template>
    <section class="detail-section">
      <h3>任务概览</h3>
      <dl>
        <div><dt>创建时间</dt><dd>{{ current.created_at ? formatStampFull(current.created_at) : '—' }}</dd></div>
        <div><dt>开始时间</dt><dd>{{ current.started_at ? formatStampFull(current.started_at) : '—' }}</dd></div>
        <div><dt>结束时间</dt><dd>{{ current.finished_at ? formatStampFull(current.finished_at) : '—' }}</dd></div>
        <div><dt>索引范围</dt><dd>{{ scopeText }}</dd></div>
        <div><dt>提取方式</dt><dd>{{ current.strategy==='reuse' ? '逐文件复用原配置' : modeLabel(current.options.extraction_mode) }}{{ current.options.mixed_invoice ? ' · 混贴票据页' : '' }}</dd></div>
        <div><dt>缓存策略</dt><dd>{{ current.options.refresh_ocr ? '忽略缓存重新识别' : '复用有效缓存' }}</dd></div>
        <div><dt>文件进度</dt><dd>{{ current.progress.done }}/{{ current.progress.total }}</dd></div>
        <div v-if="summary"><dt>处理结果</dt><dd>{{ current.published ? '入库' : '候选写入' }} {{ summary.indexed_files ?? 0 }} · 跳过 {{ summary.skipped_files ?? 0 }} · 失败 {{ summary.failed_files ?? 0 }}</dd></div>
        <div v-if="summary?.vectors"><dt>向量数量</dt><dd>{{ summary.vectors }}</dd></div>
        <div v-if="pageStats?.total"><dt>页面统计</dt><dd>成功 {{ pageStats.ok }}/{{ pageStats.total }} · 失败 {{ pageStats.failed }} · 缓存 {{ pageStats.cached }}</dd></div>
      </dl>
    </section>
    <section v-if="summary?.index_version || summary?.previous" class="detail-section">
      <h3>索引版本</h3>
      <dl><div><dt>本次版本</dt><dd class="mono">{{ summary?.index_version || '—' }}</dd></div><div v-if="summary?.previous"><dt>发布前版本</dt><dd class="mono">{{ summary.previous }}</dd></div></dl>
    </section>
    <p v-if="current.error" class="notice notice--bad" role="alert">{{ current.error }}</p>
    <p v-else-if="summary?.message" class="notice">{{ summary.message }}</p>
    <p v-if="current.status==='failed'" class="hint">本次候选结果未发布，原有索引继续可用。</p>
    <section class="detail-section">
      <div class="section-heading"><h3>文件明细 <span>{{ total }}</span></h3><button type="button" :disabled="loading" @click="load(offset)">刷新</button></div>
      <p v-if="error" class="notice notice--bad" role="alert">{{ error }}</p>
      <p v-if="loading" class="hint" role="status">正在读取文件明细…</p>
      <p v-else-if="!error && !files.length" class="hint">暂无文件处理结果。</p>
      <article v-for="row in files" :key="row.key" class="file-detail">
        <div class="file-detail__heading"><h4>{{ row.key.split('/').pop() }}</h4><span class="job-tag" :class="{'job-tag--bad':row.status==='failed','job-tag--muted':row.status==='skipped'}">{{ fileStatus(row) }}</span></div>
        <p class="file-detail__path">{{ row.key }}</p>
        <dl>
          <div><dt>提取配置</dt><dd>{{ modeLabel(row.extraction_options?.extraction_mode || current.options.extraction_mode) }}{{ row.extraction_options?.mixed_invoice ? ' · 混贴票据页' : '' }}</dd></div>
          <div v-if="row.method"><dt>实际提取</dt><dd>{{ row.method.includes('ocr') ? 'OCR' : '原生文本' }}</dd></div>
          <div v-if="row.pages?.total"><dt>页面结果</dt><dd>成功 {{ row.pages.ok }}/{{ row.pages.total }} · 空白 {{ row.pages.blank }} · 失败 {{ row.pages.failed }} · 缓存 {{ row.pages.cached }}</dd></div>
        </dl>
        <p v-if="row.reason" class="file-detail__reason" :class="{'is-bad':row.status==='failed'}">{{ row.reason }}</p>
        <p v-if="row.failed_pages?.length" class="is-bad">失败页：{{ row.failed_pages.join('、') }}</p>
        <p v-for="(warning,index) in row.warnings" :key="index" class="hint">{{ warning }}</p>
        <button v-if="canRetry && row.status!=='indexed'" type="button" @click="emit('retry',row)">用当前选择的方式重试</button>
      </article>
    </section>
    <template #footer>
      <div class="pagination"><span>{{ total ? `${offset+1}–${Math.min(offset+size,total)} / ${total}` : '0 个文件' }}</span><div><button type="button" :disabled="loading || offset===0" @click="load(Math.max(0,offset-size))">上一页</button><button type="button" :disabled="loading || offset+size>=total" @click="load(offset+size)">下一页</button></div></div>
    </template>
  </SideDrawer>
</template>

<style scoped>
.detail-section {margin:0 0 26px;}
h3 {margin:0 0 12px;font-size:13px;font-weight:600;} h3 span {color:var(--muted);font-size:12px;margin-left:5px;}
dl {margin:0;font-size:12px;} dl>div {display:grid;grid-template-columns:92px minmax(0,1fr);gap:16px;padding:8px 0;border-bottom:1px dashed var(--line);line-height:1.6;}
dt {color:var(--muted);} dd {margin:0;text-align:right;overflow-wrap:anywhere;}
.mono {font:11px/1.7 "IBM Plex Mono",ui-monospace,monospace;}
.job-tag {display:inline-block;flex:none;border-radius:999px;padding:2px 8px;background:var(--primary-tint);color:var(--primary-strong);font-size:11px;font-weight:500;white-space:nowrap;}
.job-tag--bad {background:var(--seal-tint);color:var(--seal);} .job-tag--muted {background:var(--surface-2);color:var(--muted);}
.notice {padding:12px;border:1px solid var(--line);border-radius:var(--radius-sm);font-size:12px;line-height:1.7;overflow-wrap:anywhere;background:var(--surface-2);}
.notice--bad {color:var(--seal);background:var(--seal-tint);} .hint {color:var(--muted);font-size:12px;line-height:1.7;overflow-wrap:anywhere;}
.section-heading,.file-detail__heading {display:flex;justify-content:space-between;align-items:center;gap:12px;} .section-heading {margin-bottom:14px;} .section-heading h3 {margin:0;}
.file-detail {border:1px solid var(--line);border-radius:var(--radius-sm);padding:14px;margin-top:12px;}
h4 {margin:0;font-size:13px;line-height:1.6;overflow-wrap:anywhere;min-width:0;}
.file-detail__path {font-size:11px;color:var(--muted);line-height:1.7;overflow-wrap:anywhere;margin:5px 0 10px;}
.file-detail__reason {font-size:12px;line-height:1.7;overflow-wrap:anywhere;color:var(--muted);margin:10px 0 0;}
.is-bad {color:var(--seal);font-size:12px;}
button {border:1px solid var(--line);border-radius:var(--radius-xs);background:var(--surface);color:var(--ink);padding:6px 10px;font:inherit;font-size:12px;cursor:pointer;}
button:hover:not(:disabled) {border-color:var(--primary);color:var(--primary);} button:disabled {opacity:.45;cursor:not-allowed;} button:focus-visible {outline:2px solid var(--focus);outline-offset:2px;}
.pagination {display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:12px;color:var(--muted);}.pagination>div {display:flex;gap:8px;}
@media(max-width:420px) {dl>div {grid-template-columns:72px minmax(0,1fr);gap:10px;}.file-detail__heading {align-items:flex-start;flex-wrap:wrap;}}
</style>
