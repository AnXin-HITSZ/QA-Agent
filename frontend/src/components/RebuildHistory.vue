<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { createRebuildJob, getRebuildPlan, getIndexJobHistory, getIndexJobFiles,
  type RebuildPlan, type IndexOptions, type ExtractionMode, type IndexJobStatus, type IndexJobFileRow } from "../api";
import { useKnowledge, EXTRACTION_MODES, modeLabel } from "../composables/useKnowledge";
import { errorText, formatStampFull } from "../lib/format";
import SelectField from "./SelectField.vue";

const { prefix, jobRunning, trackJob } = useKnowledge();
const plan = ref<RebuildPlan | null>(null);
const busy = ref(false);
const error = ref("");
const overrides = ref<Record<string, IndexOptions>>({});
const unknown = computed(() => plan.value?.files.filter(f => f.status === "unknown") ?? []);
const ready = computed(() => plan.value && !plan.value.files.some(f => f.status === "missing") &&
  unknown.value.every(f => overrides.value[f.key]?.extraction_mode));
const modeOptions = [{value:"",label:"请选择提取方式"},...EXTRACTION_MODES];
let generation = 0;
watch(prefix, () => {generation++;plan.value=null;overrides.value={};});
async function preview(all: boolean) {
  const request = ++generation;
  busy.value=true;error.value="";plan.value=null;overrides.value={};
  try {
    const result = await getRebuildPlan({kind:"prefix",prefix:all ? "" : prefix.value});
    if (request === generation) plan.value=result;
  } catch(e) {if (request === generation) error.value=errorText(e);}
  finally {busy.value=false;}
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
const details = ref<IndexJobFileRow[]>([]);
const detailsTotal = ref(0);
const detailsOffset = ref(0);
const detailsBusy = ref(false);
let historyGeneration=0, detailGeneration=0;
async function loadHistory(start=0) {
  const request=++historyGeneration;historyBusy.value=true;historyError.value="";
  try {
    const page=await getIndexJobHistory(start,20,filter.value);
    if(request===historyGeneration) {history.value=page.items;total.value=page.total;offset.value=start;}
  } catch(e) {if(request===historyGeneration) historyError.value=errorText(e);}
  finally {if(request===historyGeneration) historyBusy.value=false;}
}
async function showDetails(item:IndexJobStatus, start=0) {
  const request=++detailGeneration;selected.value=item;details.value=[];detailsBusy.value=true;
  try {
    const page=await getIndexJobFiles(item.job_id,start,20);
    if(request===detailGeneration) {details.value=page.items;detailsTotal.value=page.total;detailsOffset.value=start;}
  } catch(e) {if(request===detailGeneration) historyError.value=errorText(e);}
  finally {if(request===detailGeneration) detailsBusy.value=false;}
}
const labels:Record<string,string>={queued:"排队中",running:"进行中",published:"已发布",failed:"未发布"};
watch(filter,()=>void loadHistory());
onMounted(()=>void loadHistory());
</script>

<template>
  <div class="maintenance">
    <h3>按原方式重建</h3>
    <p class="note">复用当前生效版本中各文件的提取方式，使用当前切块策略。默认复用有效缓存，整批完成后统一发布。</p>
    <div class="actions">
      <button :disabled="busy || jobRunning" @click="preview(true)">查看整库重建计划</button>
      <button :disabled="busy || jobRunning || !prefix" @click="preview(false)">查看当前分类重建计划</button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div v-if="plan" class="plan">
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
      <p class="note">重建可能产生 OCR／Embedding 调用费用。未支持的文件不纳入正文索引，覆盖范围会如实显示。</p>
      <button class="primary" :disabled="busy || jobRunning || !ready || !plan.total" @click="run">确认计划并开始重建</button>
    </div>

    <h3>索引任务历史</h3>
    <div class="actions">
      <SelectField v-model="filter" :options="[{value:'',label:'全部状态'},{value:'published',label:'已发布'},{value:'failed',label:'未发布'},{value:'running',label:'进行中'},{value:'queued',label:'排队中'}]" />
      <button :disabled="historyBusy" @click="loadHistory(offset)">刷新</button><span class="note">共 {{ total }} 个任务</span>
    </div>
    <p v-if="historyError" class="error" role="alert">{{ historyError }}</p>
    <p v-if="!history.length && !historyBusy" class="note">暂无索引任务记录。</p>
    <div v-else class="scroll"><table><thead><tr><th>时间／任务</th><th>范围与方式</th><th>结果</th><th></th></tr></thead>
      <tbody><tr v-for="item in history" :key="item.job_id">
        <td>{{ item.created_at ? formatStampFull(item.created_at) : '—' }}<br/><span class="note">{{ item.job_id }}</span></td>
        <td>{{ item.scope.kind==='keys' ? `${item.scope.keys?.length || 0} 个选定文件` : item.scope.prefix || '整库' }}<br/><span class="note">{{ item.strategy==='reuse' ? '逐文件复用原配置' : modeLabel(item.options.extraction_mode) }}</span></td>
        <td>{{ labels[item.status] }} · {{ item.summary?.indexed_files ?? item.progress.done }}/{{ item.summary?.total_files ?? item.progress.total }}</td>
        <td><button @click="showDetails(item)">明细</button></td>
      </tr></tbody></table></div>
    <div class="actions"><button :disabled="historyBusy || offset===0" @click="loadHistory(Math.max(0,offset-20))">上一页</button><button :disabled="historyBusy || offset+20>=total" @click="loadHistory(offset+20)">下一页</button></div>
    <div v-if="selected" class="plan">
      <div class="actions"><strong>{{ selected.job_id }} · {{ labels[selected.status] }}</strong><button @click="selected=null;detailGeneration++">收起</button></div>
      <p v-if="selected.error" class="error">{{ selected.error }}</p>
      <p class="note">发布版本：{{ selected.summary?.index_version || '—' }} · 共 {{ detailsTotal }} 个文件</p>
      <div class="scroll"><table><thead><tr><th>文件</th><th>提取方式</th><th>结果／说明</th></tr></thead><tbody>
        <tr v-for="row in details" :key="row.key"><td class="path">{{ row.key }}</td><td>{{ modeLabel(row.extraction_options?.extraction_mode || selected.options.extraction_mode) }}{{ row.extraction_options?.mixed_invoice ? ' · 混贴票据' : '' }}</td><td>{{ row.status==='indexed' ? `候选写入 ${row.chunks} 块` : row.status==='failed' ? '失败' : '跳过' }}<br/><span class="note">{{ row.reason }}</span></td></tr>
      </tbody></table></div>
      <div class="actions"><button :disabled="detailsBusy || detailsOffset===0" @click="showDetails(selected,Math.max(0,detailsOffset-20))">上一页</button><button :disabled="detailsBusy || detailsOffset+20>=detailsTotal" @click="showDetails(selected,detailsOffset+20)">下一页</button></div>
    </div>
  </div>
</template>

<style scoped>
.maintenance {margin-bottom:20px;color:var(--ink);font-size:13px;}
h3 {font-size:14px;margin:16px 0 8px;}
.note {color:var(--muted);font-size:12px;line-height:1.7;overflow-wrap:anywhere;}
.actions,.groups {display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0;}
.groups span {padding:3px 8px;border-radius:999px;background:var(--primary-tint);color:var(--primary-strong);font-size:12px;}
button {font:inherit;border:1px solid var(--line);border-radius:8px;padding:6px 10px;background:var(--surface);color:var(--ink);cursor:pointer;}
button:hover {background:var(--primary-tint);}
button:disabled {opacity:.5;cursor:not-allowed;}
.primary {background:var(--primary-tint);color:var(--primary-strong);}
.plan {border:1px solid var(--line);border-radius:10px;padding:12px;margin:12px 0;}
.scroll {overflow:auto;max-height:360px;}
table {width:100%;border-collapse:collapse;text-align:left;}
th,td {padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top;}
th {font-weight:500;color:var(--muted);font-size:12px;}
.path {overflow-wrap:anywhere;min-width:150px;}
.error {color:var(--seal);overflow-wrap:anywhere;}
label {display:flex;align-items:center;gap:4px;margin-top:6px;}
</style>
