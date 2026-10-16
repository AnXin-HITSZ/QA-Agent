<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { createRebuildJob, getRebuildPlan, getIndexJobHistory,
  type RebuildPlan, type IndexOptions, type ExtractionMode, type IndexJobStatus } from "../api";
import { useKnowledge, EXTRACTION_MODES, modeLabel } from "../composables/useKnowledge";
import { errorText, formatStampFull } from "../lib/format";
import SelectField from "./SelectField.vue";
import IndexJobDetail from "./IndexJobDetail.vue";

const { prefix, jobRunning, trackJob } = useKnowledge();
const plan = ref<RebuildPlan | null>(null);
const planOpen = ref(false);
const allPlanOpen = computed(() => planOpen.value && !!plan.value && !plan.value.scope.prefix);
const categoryPlanOpen = computed(() => planOpen.value && !!plan.value?.scope.prefix);
const busy = ref(false);
const error = ref("");
const overrides = ref<Record<string, IndexOptions>>({});
const unknown = computed(() => plan.value?.files.filter(f => f.status === "unknown") ?? []);
const ready = computed(() => plan.value && !plan.value.files.some(f => f.status === "missing") &&
  unknown.value.every(f => overrides.value[f.key]?.extraction_mode));
const modeOptions = [{value:"",label:"请选择提取方式"},...EXTRACTION_MODES];
let generation = 0;
watch(prefix, () => {generation++;busy.value=false;plan.value=null;planOpen.value=false;overrides.value={};});
function togglePlan(all: boolean) {
  if (plan.value && (plan.value.scope.prefix || "") === (all ? "" : prefix.value)) {
    planOpen.value = !planOpen.value;
    return;
  }
  void preview(all);
}
async function preview(all: boolean) {
  const request = ++generation;
  busy.value=true;error.value="";plan.value=null;overrides.value={};
  try {
    const result = await getRebuildPlan({kind:"prefix",prefix:all ? "" : prefix.value});
    if (request === generation) {plan.value=result;planOpen.value=true;}
  } catch(e) {if (request === generation) error.value=errorText(e);}
  finally {if(request===generation) busy.value=false;}
}
function choose(key:string, mode:string) {
  if (!mode) {delete overrides.value[key];return;}
  overrides.value[key]={extraction_mode:mode as ExtractionMode,mixed_invoice:false,refresh_ocr:false};
}
async function run() {
  if (!plan.value || !ready.value || jobRunning.value) return;
  busy.value=true;error.value="";
  try {
    const result=await createRebuildJob(plan.value,Object.entries(overrides.value).map(([key,options])=>({key,options})));
    plan.value=null;
    await trackJob(result);
    await loadHistory(0);
  } catch(e) {error.value=errorText(e);}
  finally {busy.value=false;}
}

const history = ref<IndexJobStatus[]>([]);
const total = ref(0);
const offset = ref(0);
const filter = ref("");
const historyBusy = ref(false);
const historyError = ref("");
const selected = ref<IndexJobStatus | null>(null);
const pageSize=10;
let historyGeneration=0;
async function loadHistory(start=0) {
  const request=++historyGeneration;historyBusy.value=true;historyError.value="";
  try {
    const page=await getIndexJobHistory(start,pageSize,filter.value);
    if(request===historyGeneration) {history.value=page.items;total.value=page.total;offset.value=start;}
  } catch(e) {if(request===historyGeneration) historyError.value=errorText(e);}
  finally {if(request===historyGeneration) historyBusy.value=false;}
}
function showDetails(item:IndexJobStatus) {selected.value=item;}
const labels:Record<string,string>={queued:"排队中",running:"进行中",published:"已发布",failed:"未发布"};
watch(filter,()=>void loadHistory());
onMounted(()=>void loadHistory());
</script>

<template>
  <div class="maintenance">
    <section class="maintenance__section">
    <header class="section-header"><h3>按原方式重建</h3><span class="note">先查看计划，再确认执行</span></header>
    <div class="section-body">
    <p class="note">复用当前生效版本中各文件的提取方式，使用当前切块策略。默认复用有效缓存，整批完成后统一发布。</p>
    <div class="actions">
      <button :class="{'is-active':allPlanOpen}" :aria-expanded="allPlanOpen" aria-controls="rebuild-plan-preview" :disabled="busy || jobRunning" @click="togglePlan(true)">{{ allPlanOpen ? '收起整库重建计划' : '查看整库重建计划' }}</button>
      <button :class="{'is-active':categoryPlanOpen}" :aria-expanded="categoryPlanOpen" aria-controls="rebuild-plan-preview" :disabled="busy || jobRunning || !prefix" @click="togglePlan(false)">{{ categoryPlanOpen ? '收起当前分类重建计划' : '查看当前分类重建计划' }}</button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <section v-if="plan" v-show="planOpen" id="rebuild-plan-preview" class="plan" aria-labelledby="rebuild-plan-title">
      <header class="plan__header"><h4 id="rebuild-plan-title">{{ plan.scope.prefix ? '当前分类重建计划' : '整库重建计划' }}</h4><div class="plan__tools"><span class="note">由上方“{{ plan.scope.prefix ? '查看当前分类重建计划' : '查看整库重建计划' }}”生成 · 待确认</span><button :disabled="busy || jobRunning" @click="preview(!plan.scope.prefix)">刷新计划</button></div></header>
      <div class="plan__body">
      <p>范围：{{ plan.scope.prefix || '整个知识库' }} · {{ plan.total }} 个文件 · 待确认 {{ plan.unknown }} · 不支持 {{ plan.unsupported }}</p>
      <p class="note">生效版本：{{ plan.active }}</p>
      <div class="groups"><span v-for="(count, mode) in plan.groups" :key="mode">{{ modeLabel(String(mode).split('+')[0]) }}{{ String(mode).includes('+') ? ' · 混贴票据' : '' }}：{{ count }}</span></div>
      <div v-if="plan.files.length" class="scroll">
        <table><thead><tr><th>文件</th><th>提取配置</th><th>状态</th></tr></thead>
          <tbody><tr v-for="file in plan.files" :key="file.key">
            <td class="path">{{ file.key }}</td>
            <td v-if="file.status==='unknown'">
              <SelectField :model-value="overrides[file.key]?.extraction_mode || ''" :options="modeOptions" @update:model-value="choose(file.key,$event)" />
              <label v-if="overrides[file.key]?.extraction_mode==='invoice'"><input v-model="overrides[file.key]!.mixed_invoice" type="checkbox" />混贴票据页</label>
            </td>
            <td v-else>{{ file.extraction_options ? modeLabel(file.extraction_options.extraction_mode) + (file.extraction_options.mixed_invoice ? ' · 混贴票据' : '') : '—' }}</td>
            <td>{{ file.status==='known' ? '已恢复配置' : file.status==='unknown' ? '需选择方式' : file.status==='unsupported' ? '跳过并记录原因' : '原件已不存在' }}</td>
          </tr></tbody>
        </table>
      </div>
      </div>
      <footer class="plan__footer">
      <p class="note">重建可能产生 OCR／Embedding 调用费用。未支持的文件不纳入正文索引，覆盖范围会如实显示。</p>
      <button class="primary" :disabled="busy || jobRunning || !ready || !plan.total" @click="run">确认计划并开始重建</button>
      </footer>
    </section>
    </div>
    </section>

    <section class="maintenance__section">
    <header class="section-header"><h3>索引任务历史 <span class="count">{{ total }}</span></h3><span class="note">按时间倒序 · 点击记录查看详情</span></header>
    <div class="history-filters">
      <SelectField v-model="filter" :options="[{value:'',label:'全部状态'},{value:'published',label:'已发布'},{value:'failed',label:'未发布'},{value:'running',label:'进行中'},{value:'queued',label:'排队中'}]" />
      <button :disabled="historyBusy" @click="loadHistory(offset)">刷新</button><span v-if="historyBusy" class="note" role="status">载入中…</span>
    </div>
    <p v-if="historyError" class="error" role="alert">{{ historyError }}</p>
    <p v-if="!history.length && !historyBusy" class="note">暂无索引任务记录。</p>
    <div v-else class="history-scroll"><table class="history-table"><thead><tr><th>时间／任务</th><th>范围与方式</th><th>状态／处理进度</th></tr></thead>
      <tbody><tr v-for="item in history" :key="item.job_id" :class="{'is-selected':selected?.job_id===item.job_id}" tabindex="0" aria-haspopup="dialog" :aria-label="`查看任务 ${item.job_id} 的详情`" @click="showDetails(item)" @keydown.enter.prevent="showDetails(item)" @keydown.space.prevent="showDetails(item)">
        <td>{{ item.created_at ? formatStampFull(item.created_at) : '—' }}<br/><span class="note">{{ item.job_id }}</span></td>
        <td>{{ item.scope.kind==='keys' ? `${item.scope.keys?.length || 0} 个选定文件` : item.scope.prefix || '整库' }}<br/><span class="note">{{ item.strategy==='reuse' ? '逐文件复用原配置' : modeLabel(item.options.extraction_mode) }}</span></td>
        <td><span class="status" :class="{'status--bad':item.status==='failed','status--muted':item.status==='queued'}">{{ labels[item.status] }}</span><span class="history-count">{{ item.progress.done }}/{{ item.summary?.total_files ?? item.progress.total }}</span></td>
      </tr></tbody></table></div>
    <div class="history-pagination"><span class="note">{{ total ? `${offset+1}–${Math.min(offset+pageSize,total)} / ${total}` : '0 个任务' }}</span><div class="actions"><button :disabled="historyBusy || offset===0" @click="loadHistory(Math.max(0,offset-pageSize))">上一页</button><button :disabled="historyBusy || offset+pageSize>=total" @click="loadHistory(offset+pageSize)">下一页</button></div></div>
    </section>
    <IndexJobDetail v-if="selected" :key="selected.job_id" :job="selected" @close="selected=null" />
  </div>
</template>

<style scoped>
.maintenance {display:grid;gap:16px;color:var(--ink);font-size:13px;min-width:0;}
.maintenance__section {min-width:0;border:1px solid var(--line);border-radius:var(--radius-sm);background:var(--surface);}
.section-header {display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:16px 18px;border-bottom:1px solid var(--line);}
.section-header h3 {display:flex;align-items:center;gap:8px;font-size:14px;margin:0;font-weight:600;}
.section-body {padding:4px 18px 16px;}
.count {font-size:11px;font-weight:400;color:var(--primary-strong);background:var(--primary-tint);padding:1px 6px;border-radius:5px;}
.history-filters {display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 18px;}
.history-scroll {overflow-x:auto;}
.history-table {min-width:580px;}
.history-table th {background:var(--surface-2);font-size:12px;padding:11px 16px;white-space:nowrap;}
.history-table td {padding:13px 16px;font-size:12px;line-height:1.7;}
.history-table tbody tr {cursor:pointer;}.history-table tbody tr:hover,.history-table tbody tr.is-selected {background:var(--primary-tint);}
.history-table tbody tr:focus-visible {outline:2px solid var(--focus);outline-offset:-2px;background:var(--primary-tint);}
.history-table tbody tr:last-child td {border-bottom:0;}
.history-table td:last-child {white-space:nowrap;}
.history-count {margin-left:8px;color:var(--muted);white-space:nowrap;}
.status {display:inline-block;padding:1px 7px;border-radius:99px;color:var(--primary-strong);background:var(--primary-tint);font-size:11px;white-space:nowrap;}.status--bad {background:var(--seal-tint);color:var(--seal);}.status--muted {background:var(--surface-2);color:var(--muted);}
.history-pagination {display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 18px;border-top:1px solid var(--line);}.history-pagination .actions {margin:0;}
.maintenance__section>p {padding:0 18px;}
.note {color:var(--muted);font-size:12px;line-height:1.7;overflow-wrap:anywhere;}
.actions,.groups {display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0;}
.groups span {padding:3px 8px;border-radius:999px;background:var(--primary-tint);color:var(--primary-strong);font-size:12px;}
button {font:inherit;border:1px solid var(--line);border-radius:8px;padding:6px 10px;background:var(--surface);color:var(--ink);cursor:pointer;transition:background-color .18s ease,color .18s ease,border-color .18s ease,box-shadow .18s ease;}
button:hover:not(:disabled) {background:var(--primary-tint);border-color:var(--primary);}
button.is-active {background:var(--primary-tint);border-color:var(--primary);color:var(--primary-strong);}
button:disabled {opacity:.5;cursor:not-allowed;}
button:focus-visible {outline:2px solid var(--focus);outline-offset:2px;}
.primary {background:var(--primary);border-color:var(--primary);color:#fff;}
.primary:hover:not(:disabled) {background:var(--primary-strong);border-color:var(--primary-strong);box-shadow:0 2px 6px var(--primary-tint);}
.primary:active:not(:disabled) {background:var(--primary);box-shadow:none;}
.plan {border:1px solid var(--line);border-left:3px solid var(--primary);border-radius:10px;margin:14px 0 0 12px;overflow:hidden;}
.plan__header {display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;padding:12px 14px;background:var(--primary-tint);border-bottom:1px solid var(--line);}
.plan__header h4 {margin:0;font-size:13px;font-weight:600;color:var(--primary-strong);}
.plan__tools {display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap;margin-left:auto;}
.plan__body {padding:0 14px 14px;}
.plan__footer {display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:12px 14px;border-top:1px solid var(--line);background:var(--surface-2);}
.plan__footer .note {flex:1 1 280px;margin:0;}
.plan__footer button {flex-shrink:0;margin-left:auto;}
.scroll {overflow:auto;max-height:360px;}
table {width:100%;border-collapse:collapse;text-align:left;}
th,td {padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top;}
th {font-weight:500;color:var(--muted);font-size:12px;}
.path {overflow-wrap:anywhere;min-width:150px;}
.error {color:var(--seal);overflow-wrap:anywhere;}
label {display:flex;align-items:center;gap:4px;margin-top:6px;}
@media(max-width:640px) {.section-header {padding:14px;}.section-body {padding:4px 14px 14px;}.history-filters,.history-pagination {padding:12px 14px;}.section-header>.note {font-size:11px;}}
@media(max-width:640px) {.plan {margin-left:0;}.plan__header,.plan__footer {padding:12px;}.plan__body {padding:0 12px 12px;}}
@media(prefers-reduced-motion:reduce) {button {transition:none;}}
</style>
