<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue';
import { apiFetch } from '../stores/auth';
import { formatStampFull } from '../lib/format';

type Details = { amount: string | null; currency: string; purpose: string; project: string; expense_date: string; handler: string; policy_ref: string; required_materials: string[] };
type Link = { id: string; kind: string; title: string; details: { state: string; source_ref?: string; automatic?: boolean }; revision: number };
type Event = { id: string; kind: string; description: string; source_type: string; source_ref: string; created_at: string };
type Matter = { id: string; title: string; status: string; revision: number; details: Details; updated_at: string; links: Link[]; events: Event[]; missing_materials: string[]; projection: { pending: boolean; error: string } };
type Graph = { nodes: { id: string; kind: string; title: string }[]; edges: { source: string; target: string; label: string }[]; truncated_events: boolean; omitted_nodes?:number; degraded?:string };
const items=ref<Matter[]>([]), selected=ref<Matter|null>(null), graph=ref<Graph|null>(null), error=ref(''), busy=ref(false), filter=ref('');
const tab=ref('details'), creating=ref(false), title=ref(''), details=ref<Details>(empty()), requirements=ref(''), draftText=ref('');
const action=ref('note'), description=ref(''), source=ref(''), linkKind=ref('material'), linkTitle=ref(''), linkState=ref('reported');
const editingLink=ref('');
const offset=ref(0), pageSize=50;
const labels:Record<string,string>={preparing:'准备中',submitted:'已提交（用户确认）',returned:'已退回',approved:'已批准（用户确认）',paid:'已支付（用户确认）',cancelled:'已取消'};
const actions:Record<string,string>={submit:'确认已提交',return:'记录退回',resubmit:'确认重新提交',approve:'确认审批通过',pay:'确认已支付',cancel:'取消事项',note:'补充说明',correct:'更正历史说明'};
const types:Record<string,string>={matter:'事项',material:'材料',task:'待办',project:'项目',person:'人员',event:'办理事件'};
const states:Record<string,string>={reported:'已声明',provided:'已提供引用',verified:'用户确认已核验',pending:'待完成',done:'已完成'};
const linkStates=computed(()=>({material:['reported','provided','verified'],task:['pending','done'],project:['reported'],person:['reported']} as Record<string,string[]>)[linkKind.value]||['reported']);
watch(linkKind,()=>{if(!linkStates.value.includes(linkState.value))linkState.value=linkStates.value[0]||'reported';});
const allowed=computed(()=>{
 const state=selected.value?.status;
 return Object.entries(actions).filter(([key])=>['note','correct'].includes(key)||({submit:['preparing'],return:['submitted','approved'],resubmit:['returned'],approve:['submitted'],pay:['approved'],cancel:['preparing','submitted','returned']} as Record<string,string[]>)[key]?.includes(state||''));
});
function empty():Details { return {amount:null,currency:'CNY',purpose:'',project:'',expense_date:'',handler:'',policy_ref:'',required_materials:[]}; }
async function request(path:string,method='GET',data?:unknown) {
 const res=await apiFetch('/api/v1/matters'+path,{method,headers:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});
 if(!res.ok){ const problem=await res.json();throw new Error(typeof problem.detail==='string'?problem.detail:'字段校验失败，请检查输入'); }
 return await res.json();
}
async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn();}catch(e){error.value=e instanceof Error?e.message:String(e);}finally{busy.value=false;}}
async function load(){items.value=(await request(`?status=${encodeURIComponent(filter.value)}&limit=${pageSize}&offset=${offset.value}`)).items;}
async function open(id:string){selected.value=await request('/'+id);creating.value=false;editingLink.value='';title.value=selected.value!.title;details.value={...selected.value!.details};requirements.value=details.value.required_materials.join('\n');graph.value=await request('/'+id+'/graph');action.value='note';}
function newMatter(){creating.value=true;selected.value=null;editingLink.value='';source.value='';description.value='';title.value='';details.value=empty();requirements.value='';tab.value='details';}
async function save(){await run(async()=>{details.value.required_materials=requirements.value.split('\n').map(s=>s.trim()).filter(Boolean);const body={title:title.value,details:{...details.value,amount:details.value.amount===null||String(details.value.amount).trim()===''?null:String(details.value.amount)},request_id:crypto.randomUUID()};const result=selected.value?await request('/'+selected.value.id,'PATCH',{...body,revision:selected.value.revision}):await request('','POST',body);await load();await open(result.id);});}
async function extract(){await run(async()=>{const d=await request('/draft','POST',{text:draftText.value});title.value=d.title;details.value=d.details;requirements.value=d.details.required_materials.join('\n');});}
async function event(){await run(async()=>{const id=selected.value!.id;await request('/'+id+'/events','POST',{revision:selected.value!.revision,kind:action.value,description:description.value,source_ref:source.value,request_id:crypto.randomUUID()});description.value='';source.value='';await load();await open(id);});}
async function addLink(){await run(async()=>{const id=selected.value!.id;await request('/'+id+'/links'+(editingLink.value?'/'+editingLink.value:''),editingLink.value?'PATCH':'POST',{revision:selected.value!.revision,kind:linkKind.value,title:linkTitle.value,state:linkState.value,source_ref:source.value,request_id:crypto.randomUUID()});linkTitle.value='';editingLink.value='';await open(id);});}
async function unlink(id:string){if(!confirm('解除此关联？外部文件不会删除。'))return;await run(async()=>{await request('/'+selected.value!.id+'/links/'+id,'DELETE',{revision:selected.value!.revision,request_id:crypto.randomUUID()});await open(selected.value!.id);});}
async function remove(){if(!confirm('删除此事项？办理审计将保留，外部文件不会删除。'))return;await run(async()=>{await request('/'+selected.value!.id,'DELETE',{revision:selected.value!.revision,request_id:crypto.randomUUID()});selected.value=null;graph.value=null;await load();});}
const graphNodes=computed(()=>{
 const nodes=graph.value?.nodes||[];return nodes.map((node,i)=>({ ...node,x:i===0?400:400+300*Math.cos((i-1)*2*Math.PI/Math.max(1,nodes.length-1)),y:i===0?270:270+210*Math.sin((i-1)*2*Math.PI/Math.max(1,nodes.length-1)) }));
});
function point(id:string){return graphNodes.value.find(n=>n.id===id)||{x:400,y:270};}
function editLink(link:Link){tab.value='links';if(link.details.automatic){error.value='此待办由材料状态自动维护，请更新对应材料。';return;}editingLink.value=link.id;linkKind.value=link.kind;linkTitle.value=link.title;linkState.value=link.details.state;source.value=link.details.source_ref||'';}
function focusNode(id:string){const n=graph.value?.nodes.find(x=>x.id===id);if(n?.kind==='matter'||id.endsWith(':project')||id.endsWith(':handler'))tab.value='details';else if(n?.kind==='event'){tab.value='timeline';description.value='更正说明：'+n.title;action.value='correct';}else{const link=selected.value?.links.find(x=>x.id===id);if(link)editLink(link);}}
onMounted(()=>void run(load));
</script>

<template>
 <section class="matters">
  <header class="matters-head"><div><h1>事务管理</h1><p>管理课题组报销事项、材料与办理经历。提交、审批及支付记录均须有明确确认。</p></div><button @click="newMatter">新建报销</button></header>
  <p v-if="error" class="error" role="alert">{{error}}</p>
  <div class="matters-layout">
   <aside class="matter-list"><div class="toolbar"><select v-model="filter" @change="offset=0;run(load)"><option value="">全部状态</option><option v-for="(name,key) in labels" :key="key" :value="key">{{name}}</option></select><button :disabled="busy" @click="run(load)">刷新</button></div>
    <button v-for="item in items" :key="item.id" class="matter-row" :class="{active:selected?.id===item.id}" @click="run(()=>open(item.id))"><strong>{{item.title}}</strong><span>{{labels[item.status]}}</span><small>{{formatStampFull(item.updated_at)}}</small></button>
    <p v-if="!items.length">暂无事项</p><div class="toolbar"><button :disabled="offset===0||busy" @click="offset-=pageSize;run(load)">上一页</button><button :disabled="items.length<pageSize||busy" @click="offset+=pageSize;run(load)">下一页</button></div>
   </aside>
   <main class="matter-detail" v-if="selected||creating">
    <div v-if="selected" class="toolbar"><strong>{{labels[selected.status]}}</strong><span>版本 {{selected.revision}}</span><button class="danger" :disabled="busy" @click="remove">删除事项</button></div>
    <nav class="matter-tabs"><button v-for="(name,key) in {details:'事项详情',links:'材料与待办',timeline:'办理时间线',graph:'关联图'}" :key="key" :disabled="creating&&key!=='details'" :class="{active:tab===key}" @click="tab=key">{{name}}</button></nav>
    <form v-if="tab==='details'" @submit.prevent="save">
     <div v-if="creating" class="draft"><label>从描述整理草稿（会调用模型，保存前请核对）<textarea v-model="draftText" placeholder="例如：办公用品报销320元，发票已有" /></label><button type="button" :disabled="busy||!draftText.trim()" @click="extract">整理草稿</button></div>
     <label>事项名称<input v-model="title" required maxlength="200" /></label>
     <div class="fields"><label>金额（元）<input v-model="details.amount" type="number" min="0.01" step="0.01" /></label><label>费用日期<input v-model="details.expense_date" type="date" /></label></div>
     <label>费用用途<textarea v-model="details.purpose" maxlength="1000" /></label><div class="fields"><label>项目<input v-model="details.project" maxlength="200" /></label><label>经办人<input v-model="details.handler" maxlength="200" /></label></div>
     <label>材料要求的制度来源<input v-model="details.policy_ref" maxlength="500" placeholder="SOP编号、制度版本或可追溯引用" /></label><label>所需材料（每行一项，必须填写制度来源）<textarea v-model="requirements" /></label>
     <button :disabled="busy||!!selected&&!['preparing','returned'].includes(selected.status)">保存并生成缺件待办</button>
    </form>
    <div v-if="tab==='links'&&selected"><p v-if="selected.missing_materials.length">待补材料：{{selected.missing_materials.join('、')}}</p><p v-else>没有已登记的缺件；未登记制度要求不代表材料已完备。</p><article v-for="link in selected.links" :key="link.id" class="link-row"><strong>{{types[link.kind]}} · {{link.title}}</strong><span>{{states[link.details.state]||link.details.state}}</span><small>{{link.details.source_ref}}</small><button :disabled="busy||link.details.automatic" @click="unlink(link.id)">解除关联</button><button :disabled="busy||link.details.automatic" @click="editLink(link)">编辑</button></article>
     <form @submit.prevent="addLink"><div class="fields"><label>关联类型<select v-model="linkKind"><option v-for="kind in ['material','task','project','person']" :key="kind" :value="kind">{{types[kind]}}</option></select></label><label>状态<select v-model="linkState"><option v-for="s in linkStates" :key="s">{{states[s]}}</option></select></label></div><label>名称<input v-model="linkTitle" required maxlength="200" /></label><label>来源或文件引用<input v-model="source" maxlength="500" /></label><button :disabled="busy">{{editingLink?'保存关联修改':'添加关联'}}</button><button v-if="editingLink" type="button" @click="editingLink='';linkTitle=''">取消编辑</button></form>
    </div>
    <div v-if="tab==='timeline'&&selected"><ol class="timeline"><li v-for="e in selected.events" :key="e.id"><strong>{{e.description}}</strong><small>记录时间：{{formatStampFull(e.created_at)}} · 用户确认</small><p v-if="e.source_ref">来源：{{e.source_ref}}</p></li></ol>
     <form @submit.prevent="event"><label>办理操作<select v-model="action"><option v-for="[key,name] in allowed" :key="key" :value="key">{{name}}</option></select></label><label>记录或更正说明<textarea v-model="description" required maxlength="4000" /></label><label>确认来源<input v-model="source" maxlength="500" placeholder="审批、支付、退回须提供来源" /></label><button :disabled="busy">确认并记录</button></form>
    </div>
    <div v-if="tab==='graph'&&selected"><p>点击节点查看对应对象。图由权威事务数据生成；编辑后同步 Neo4j。</p><p v-if="selected.projection.pending">图投影待同步{{selected.projection.error?'：'+selected.projection.error:''}}，当前展示 MySQL 的最新关系。</p><p v-if="graph?.degraded">{{graph.degraded}}</p><p v-if="graph?.omitted_nodes">图中省略{{graph.omitted_nodes}}个对象，请到详情查看完整记录。</p><p v-if="graph?.truncated_events">仅展示最近50条办理事件，完整历史见时间线。</p>
     <svg class="matter-graph" viewBox="0 0 800 540" role="group" aria-label="当前事务关联图"><defs><marker id="matter-arrow" markerWidth="8" markerHeight="8" refX="24" refY="3" orient="auto"><path d="M0,0 L0,6 L6,3 z" fill="currentColor"/></marker></defs><g v-for="(edge,i) in graph?.edges" :key="i"><line :x1="point(edge.source).x" :y1="point(edge.source).y" :x2="point(edge.target).x" :y2="point(edge.target).y" marker-end="url(#matter-arrow)" /><text :x="(point(edge.source).x+point(edge.target).x)/2" :y="(point(edge.source).y+point(edge.target).y)/2">{{edge.label}}</text></g><g v-for="node in graphNodes" :key="node.id" :transform="`translate(${node.x},${node.y})`" tabindex="0" role="button" :aria-label="node.title" @click="focusNode(node.id)" @keydown.enter="focusNode(node.id)"><circle :r="node.kind==='matter'?43:30" :class="node.kind"/><text y="4">{{types[node.kind]}}</text><text y="51">{{node.title.slice(0,16)}}{{node.title.length>16?'…':''}}</text><title>{{node.title}}</title></g></svg>
    </div>
   </main><main v-else class="matter-detail empty">选择事项查看详情，或新建一笔报销。</main>
  </div>
 </section>
</template>

<style scoped>
.matters{flex:1;overflow-y:auto;box-sizing:border-box;width:100%;max-width:var(--maxw);margin:0 auto;padding:24px;color:var(--ink)}.matters-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}.matters h1{font-size:24px;margin:0 0 10px}.matters p,.matters small{color:var(--muted,#6a7b85);font-size:13px}.matters-layout{display:grid;grid-template-columns:290px 1fr;gap:20px}.matter-list,.matter-detail{background:var(--surface,#fff);border:1px solid var(--line);border-radius:10px;padding:20px}.toolbar,.fields,.matter-tabs{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.matter-row{display:flex;flex-direction:column;gap:7px;width:100%;text-align:left;margin:12px 0}.matters button{border:1px solid var(--line);border-radius:6px;padding:8px 12px;background:var(--surface,#fff);color:inherit;cursor:pointer}.matters button.active{border-color:var(--primary);background:var(--primary-tint)}.matters button:disabled{opacity:.5;cursor:default}.matters label{display:flex;flex-direction:column;gap:6px;margin:14px 0;font-size:13px}.matters input,.matters textarea,.matters select{padding:9px;border:1px solid var(--line);border-radius:6px;background:var(--surface,#fff);color:inherit;max-width:100%;box-sizing:border-box}.matters textarea{min-height:70px;resize:vertical}.fields>label{flex:1}.matter-tabs{border-bottom:1px solid var(--line);padding:16px 0}.error,.danger{color:#a52d32!important}.link-row{border-bottom:1px solid var(--line);padding:12px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap}.timeline{padding-left:24px}.timeline li{padding:12px;border-bottom:1px solid var(--line)}.timeline small{display:block;margin-top:6px}.matter-graph{width:100%;min-height:320px}.matter-graph line{stroke:#a9bac0}.matter-graph circle{fill:var(--surface-2);stroke:var(--primary)}.matter-graph circle.matter{fill:var(--primary-tint)}.matter-graph text{font-size:11px;fill:var(--ink);text-anchor:middle}.matter-graph g[role=button]{cursor:pointer}.empty{padding-top:100px;text-align:center}.draft{border-bottom:1px dashed var(--line);padding-bottom:16px}@media(max-width:800px){.matters-layout{grid-template-columns:1fr}.matter-list{max-height:260px;overflow:auto}.matters-head{align-items:flex-start;gap:12px}.matters{padding:0 12px}}
</style>
