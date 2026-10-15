<script setup lang="ts">
import { computed, ref } from "vue";
import type { IndexJobFileRow } from "../api";
import { useKnowledge } from "../composables/useKnowledge";
import { errorText, formatStampFull } from "../lib/format";
import RebuildHistory from "./RebuildHistory.vue";
import IndexJobDetail from "./IndexJobDetail.vue";

const {job,jobFiles,jobStarting,jobError,jobRunning,manifest,modeChosen,startJob,
  retryJobFile,rollbackToPrevious,refreshManifest} = useKnowledge();
const open = ref(false);
const detailOpen = ref(false);
const confirmingRollback = ref(false);
const rollbackErr = ref("");
const rollbackOk = ref("");
const history = computed(() => [...(manifest.value?.history ?? [])].reverse());
const failedRows = computed(() => jobFiles.value.filter(row => row.status === "failed"));
const statusText = computed(() => ({queued:"排队中",running:"进行中",published:"已发布",failed:"未发布"})[job.value?.status ?? "published"]);
const pct = computed(() => job.value?.progress.total ? Math.min(100,Math.round(job.value.progress.done/job.value.progress.total*100)) : 0);
function toggle() {open.value=!open.value;if(open.value) void refreshManifest();}
async function retryFailed() {
  await startJob({kind:"keys",keys:failedRows.value.map(row=>row.key)});
}
async function retryFile(row: IndexJobFileRow) {detailOpen.value=false;await retryJobFile(row);}
async function doRollback() {
  confirmingRollback.value=false;rollbackErr.value="";rollbackOk.value="";
  try {await rollbackToPrevious();rollbackOk.value=manifest.value?.active ?? "";}
  catch(e) {rollbackErr.value=errorText(e);}
}
</script>

<template>
  <section class="rb">
    <button type="button" class="rb__toggle" :aria-expanded="open" aria-controls="knowledge-index-maintenance" @click="toggle">
      <svg viewBox="0 0 20 20" fill="none" :class="{'is-open':open}" aria-hidden="true"><path d="m7 5 5 5-5 5" /></svg>
      <span>索引任务与维护</span>
      <span v-if="jobRunning" class="rb__badge">进行中 {{ job?.progress.done }}/{{ job?.progress.total }}</span>
      <span v-else-if="job" class="rb__badge" :class="{'is-bad':job.status==='failed'}">{{ job.status==='published' ? '上次已发布' : '上次未发布' }}</span>
      <span class="rb__toggle-hint">{{ open ? '收起' : '展开' }}</span>
    </button>
    <div v-if="open" id="knowledge-index-maintenance" class="rb__body">
      <p v-if="jobError" class="rb__error" role="alert">{{ jobError }}</p>
      <section v-if="job" class="rb__current">
        <header class="rb__current-heading">
          <div><h3>{{ jobRunning ? '当前索引任务' : '最近索引任务' }} <span class="rb__badge" :class="{'is-bad':job.status==='failed'}">{{ statusText }}</span></h3><p class="rb__id">{{ job.job_id }}</p></div>
          <button type="button" class="rb__button" @click="detailOpen=true">查看明细 <span aria-hidden="true">↗</span></button>
        </header>
        <div class="rb__progress" role="progressbar" aria-label="索引文件处理进度" :aria-valuenow="job.progress.done" :aria-valuemin="0" :aria-valuemax="Math.max(1,job.progress.total)"><span :style="{width:pct+'%'}" /></div>
        <div class="rb__metrics"><span>已处理 <strong>{{ job.progress.done }}/{{ job.progress.total }}</strong></span><template v-if="job.summary"><span>{{ job.published ? '入库' : '候选写入' }} <strong>{{ job.summary.indexed_files ?? 0 }}</strong></span><span>跳过 <strong>{{ job.summary.skipped_files ?? 0 }}</strong></span><span>失败 <strong>{{ job.summary.failed_files ?? 0 }}</strong></span><span v-if="job.summary.vectors">向量 <strong>{{ job.summary.vectors }}</strong></span></template></div>
        <p v-if="jobRunning && job.progress.current" class="rb__note rb__path" :title="job.progress.current">当前文件：{{ job.progress.current }}</p>
        <p v-if="job.status==='failed'" class="rb__error">本次未发布，原有索引继续可用。请在任务明细中查看原因。</p>
        <button v-if="!jobRunning && failedRows.length" type="button" class="rb__button" :disabled="!modeChosen || jobStarting" @click="retryFailed">用当前方式重试 {{ failedRows.length }} 个失败文件</button>
      </section>

      <RebuildHistory />

      <details v-if="manifest" class="rb__versions">
        <summary>版本与回退 <span>查看生效版本与切换记录</span></summary>
        <div class="rb__version-body">
          <dl><div><dt>当前生效</dt><dd>{{ manifest.active }}</dd></div><div v-if="manifest.previous"><dt>上一版本</dt><dd>{{ manifest.previous }}</dd></div></dl>
          <div v-if="manifest.previous" class="rb__actions">
            <template v-if="confirmingRollback"><span class="rb__note">确认回退到上一版本？</span><button type="button" class="rb__button rb__button--danger" :disabled="jobRunning" @click="doRollback">确认回退</button><button type="button" class="rb__button" @click="confirmingRollback=false">取消</button></template>
            <button v-else type="button" class="rb__button" :disabled="jobRunning" @click="confirmingRollback=true;rollbackOk=''">回退到上一版本</button>
          </div>
          <p v-if="rollbackErr" class="rb__error" role="alert">{{ rollbackErr }}</p><p v-else-if="rollbackOk" class="rb__note" role="status">已切回版本 {{ rollbackOk }}</p>
          <ul v-if="history.length" class="rb__history"><li v-for="(entry,index) in history" :key="index"><time>{{ formatStampFull(entry.at) }}</time><span class="rb__badge" :class="{'is-bad':entry.action==='rollback'}">{{ entry.action==='rollback' ? '回退' : '发布' }}</span><span class="rb__history-version" :title="entry.note">{{ entry.name }}</span><span v-if="entry.points != null">{{ entry.points }} 点</span></li></ul>
        </div>
      </details>
    </div>
    <IndexJobDetail v-if="detailOpen && job" :key="job.job_id" :job="job" :can-retry="!jobRunning && modeChosen && !jobStarting" @close="detailOpen=false" @retry="retryFile" />
  </section>
</template>

<style scoped>
.rb {margin-top:18px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);}
.rb__toggle {display:flex;align-items:center;gap:10px;width:100%;padding:18px;box-sizing:border-box;border:0;border-radius:var(--radius);background:transparent;color:var(--ink);font:inherit;font-size:14px;font-weight:600;text-align:left;cursor:pointer;}
.rb__toggle:hover {background:var(--surface-2);}
.rb__toggle svg {width:18px;height:18px;flex:none;stroke:var(--muted);stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round;transition:transform .15s;}.rb__toggle svg.is-open {transform:rotate(90deg);}
.rb__toggle-hint {margin-left:auto;color:var(--muted);font-size:12px;font-weight:400;}
.rb__badge {display:inline-block;padding:2px 8px;border-radius:999px;background:var(--primary-tint);color:var(--primary-strong);font-size:11px;line-height:1.6;font-weight:500;white-space:nowrap;}.rb__badge.is-bad {background:var(--seal-tint);color:var(--seal);}
.rb__body {margin:0 20px 20px 28px;padding-left:18px;border-left:2px solid var(--line);min-width:0;}
.rb__current {padding:16px;border:1px solid var(--line);border-radius:var(--radius-sm);background:var(--surface-2);margin-bottom:16px;}
.rb__current-heading {display:flex;justify-content:space-between;align-items:center;gap:12px;}
h3 {display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0;font-size:13px;}
.rb__id {margin:6px 0 0;font:11px/1.6 "IBM Plex Mono",ui-monospace,monospace;color:var(--muted);overflow-wrap:anywhere;}
.rb__progress {height:5px;background:var(--line);border-radius:99px;overflow:hidden;margin:14px 0 12px;}.rb__progress span {display:block;height:100%;background:var(--primary);border-radius:inherit;}
.rb__metrics {display:flex;flex-wrap:wrap;gap:10px 20px;color:var(--muted);font-size:12px;}.rb__metrics strong {color:var(--ink);font-weight:500;margin-left:4px;}
.rb__note {font-size:12px;line-height:1.7;color:var(--muted);overflow-wrap:anywhere;}.rb__path {overflow:hidden;white-space:nowrap;text-overflow:ellipsis;}
.rb__error {font-size:12px;line-height:1.7;color:var(--seal);overflow-wrap:anywhere;}
.rb__button {flex:none;padding:6px 10px;border:1px solid var(--line);border-radius:var(--radius-xs);background:var(--surface);color:var(--ink);font:inherit;font-size:12px;cursor:pointer;}.rb__button:hover:not(:disabled) {color:var(--primary);border-color:var(--primary);}.rb__button:disabled {opacity:.5;cursor:not-allowed;}.rb__button--danger {color:var(--seal);background:var(--seal-tint);}
button:focus-visible,summary:focus-visible {outline:2px solid var(--focus);outline-offset:3px;}
.rb__versions {border:1px solid var(--line);border-radius:var(--radius-sm);margin-top:16px;}.rb__versions summary {padding:15px 16px;cursor:pointer;font-size:13px;font-weight:500;}.rb__versions summary>span {font-weight:400;color:var(--muted);font-size:12px;margin-left:8px;}.rb__version-body {padding:0 16px 16px;}
dl {margin:0;}dl>div {display:flex;gap:12px;margin:8px 0;font-size:12px;}dt {color:var(--muted);white-space:nowrap;}dd {margin:0;overflow-wrap:anywhere;font:11px/1.7 "IBM Plex Mono",ui-monospace,monospace;}
.rb__actions {display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:14px;}
.rb__history {padding:12px 0 0;margin:16px 0 0;border-top:1px solid var(--line);list-style:none;display:grid;gap:10px;}.rb__history li {display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:11px;}.rb__history-version {overflow-wrap:anywhere;font-family:"IBM Plex Mono",ui-monospace,monospace;}
@media(max-width:640px) {.rb__toggle {padding:14px;gap:8px;flex-wrap:wrap;}.rb__toggle svg {width:16px;height:16px;}.rb__body {margin:0 12px 14px 20px;padding-left:16px;}.rb__current-heading {align-items:flex-start;}.rb__current-heading>div {min-width:0;}.rb__versions summary>span {display:block;margin:6px 0 0;}}
@media(prefers-reduced-motion:reduce) {.rb__toggle svg {transition:none;}}
</style>
