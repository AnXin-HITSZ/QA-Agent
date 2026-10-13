<script setup lang="ts">
import { ref, watch, onActivated } from 'vue';
import { apiFetch } from '../stores/auth';
const props = defineProps<{ threadId: string | null; loading: boolean }>();
type Proposal = { id:string; operation:string; status:string; matter_id:string; expires_at:string;
 payload:{request:Record<string, unknown>}; before:Record<string, unknown>; result:{id?:string} };
const items=ref<Proposal[]>([]), error=ref(''), busy=ref('');
const names:Record<string,string>={create:'创建报销事项',edit:'更新事项',event:'记录办理进展',link:'添加关联',edit_link:'更新材料或关联',unlink:'解除关联',delete:'删除事项'};
const fields:Record<string,string>={title:'名称',amount:'金额',currency:'币种',purpose:'用途',project:'项目',expense_date:'费用日期',handler:'经办人',policy_ref:'制度来源',required_materials:'所需材料',kind:'类型/操作',description:'记录说明',source_ref:'确认来源',state:'材料/待办状态'};
const values:Record<string,string>={submit:'记录用户确认已提交',return:'记录退回',resubmit:'记录重新提交',approve:'记录用户确认已批准',pay:'记录用户确认已支付',cancel:'取消',note:'补充说明',correct:'更正说明',material:'材料',task:'待办',person:'人员',project:'项目',reported:'已声明',provided:'已提供引用',verified:'用户确认已核验',pending:'待完成',done:'已完成',reimbursement:'报销'};
function rows(data:Record<string,unknown>):[string,string][] {
 const flat={...data,...(data.details as Record<string,unknown>||{})};
 return Object.entries(flat).filter(([k,v])=>k in fields&&v!==''&&v!==null).map(([k,v])=>[fields[k],Array.isArray(v)?v.join('、'):values[String(v)]||String(v)]);
}
let seq=0;
async function refresh(){
 const n=++seq; const thread=props.threadId;
 if(!thread){items.value=[];error.value='';return;}
 try{
  const res=await apiFetch('/api/v1/matters/proposals?thread_id='+encodeURIComponent(thread));
  if(!res.ok)throw new Error('无法读取事务操作，请稍后刷新');
  const data=await res.json();if(n!==seq)return;
  items.value=data.items.filter((p:Proposal)=>['pending','confirmed','rejected'].includes(p.status));error.value='';
 }catch(e){if(n===seq)error.value=e instanceof Error?e.message:String(e);}
}
async function decide(p:Proposal,confirm:boolean){
 if(busy.value)return;busy.value=p.id;error.value='';
 try{
  const res=await apiFetch(`/api/v1/matters/proposals/${p.id}/${confirm?'confirm':'reject'}`,{method:'POST'});
  if(!res.ok){const e=await res.json();throw new Error(typeof e.detail==='string'?e.detail:'操作失败');}
  await refresh();
 }catch(e){error.value=e instanceof Error?e.message:String(e);}finally{busy.value='';}
}
watch(()=>[props.threadId,props.loading],()=>{if(!props.loading)void refresh();else if(!props.threadId)items.value=[];},{immediate:true});
onActivated(()=>{if(!props.loading)void refresh();});
</script>

<template>
 <section v-if="items.length||error" class="proposals" aria-label="聊天事务操作">
  <p v-if="error" role="alert">{{error}} <button @click="refresh">重试</button></p>
  <article v-for="p in items" :key="p.id" class="proposal">
   <header><strong>{{names[p.operation]}}</strong><span>{{p.status==='pending'?'待你确认':p.status==='confirmed'?'已保存':'已拒绝'}}</span></header>
   <details v-if="Object.keys(p.before).length"><summary>变更前的事项</summary><dl><template v-for="[k,v] in rows(p.before)" :key="k"><dt>{{k}}</dt><dd>{{v}}</dd></template></dl></details>
   <dl><template v-for="[k,v] in rows(p.payload.request)" :key="k"><dt>{{k}}</dt><dd>{{v}}</dd></template></dl>
   <p>仅记录你的报销事项和办理事实，不会向学校系统提交、审批或支付。</p>
   <div v-if="p.status==='pending'" class="buttons"><button :disabled="!!busy||loading" @click="decide(p,true)">确认{{p.operation==='delete'?'删除':'保存'}}</button><button :disabled="!!busy||loading" @click="decide(p,false)">拒绝</button><span>需要修改时，直接在聊天中告诉助手。</span></div>
   <a v-else-if="p.status==='confirmed'" href="#/matters">到事务管理查看、编辑或删除</a>
  </article>
 </section>
</template>

<style scoped>
.proposals{margin:20px 0;color:var(--ink)}.proposal{border:1px solid var(--line);border-radius:12px;background:var(--surface);padding:18px;margin:12px 0}.proposal header,.buttons{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.proposal header{justify-content:space-between}.proposal span,.proposal p{font-size:12px;color:var(--muted)}dl{display:grid;grid-template-columns:100px minmax(0,1fr);gap:8px;font-size:13px}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}.proposal button{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:7px 12px;color:var(--ink);cursor:pointer}.proposal button:first-child{color:var(--primary);border-color:var(--primary)}.proposal button:disabled{opacity:.5;cursor:default}.proposal a{color:var(--primary);font-size:13px}
</style>
