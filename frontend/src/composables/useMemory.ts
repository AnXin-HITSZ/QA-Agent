// 「我的记忆」页:列表 / 检索 / 手添 / 编辑 / 删除 / 彻底清除 / 补索引 / 功能状态。
// 模块级单例:视图与状态条共享同一份状态,不层层透传。
//
// 口径(与后端 /api/v1/memory、docs/长期记忆系统技术方案.md 一致,界面文案不得偏离):
// - **归属由后端按登录身份决定**:请求里没有 user_id,前端也没有可传的位置 —— 别人的 id
//   与不存在的 id 一样返回 404;
// - 「暂停」≠「删除」:write_enabled / search_enabled / maintenance_enabled 为 false 只是
//   对应那一路行为停下来,已存记忆在这里照常看得见、改得动;
// - index_state=pending 是「向量待补」:记忆已经保存,后台会自愈,绝不写成「没保存」;
// - 检索模式没有分页:后端一次给前 N 条(top_k),total 表示本次返回条数 —— 因此检索时
//   藏起页码,别拿它当总数。
import { computed, ref } from "vue";

import {
  addMemory,
  clearMemory,
  deleteMemory,
  editMemory,
  getMemoryStatus,
  listMemory,
  reindexMemory,
  type MemoryClearResult,
  type MemoryItem,
  type MemoryStatus,
} from "../api";
import { errorText } from "../lib/format";

const PAGE_SIZE = 20;

const items = ref<MemoryItem[]>([]);
const total = ref(0); // 普通列表 = 库里的总数;检索模式 = 本次返回条数
const query = ref(""); // 已提交的检索词(空 = 普通列表)
const draft = ref(""); // 输入框里的字
const offset = ref(0);

const status = ref<MemoryStatus | null>(null);
const statusError = ref("");

const loading = ref(false);
const error = ref(""); // 列表 / 检索失败或整体不可用(文案来自后端,可读)
const degraded = ref<string[]>([]); // 本次没做到最好的地方(如实展示)
const loaded = ref(false); // 成功载入过(KeepAlive 回来时按需刷新)

const adding = ref(false);
const addText = ref("");
const addError = ref("");

const editingId = ref("");
const editDraft = ref("");
const saving = ref(false);
const editError = ref("");

const removingId = ref("");
const busyId = ref(""); // 正在请求里的那一行(删除 / 保存)
// 删除的索引收尾是**两步**:事实立刻删掉,向量清理可能要后台重试。这里记下那句话,
// 免得界面在 cleanup=pending 时假装「已经全部清干净」(见后端 service.delete_item)。
const deleteNote = ref("");

const clearing = ref(false);
const clearError = ref("");
const clearResult = ref<MemoryClearResult | null>(null);

const reindexing = ref(false);
const reindexNote = ref("");

const hasPrev = computed(() => offset.value > 0);
const hasNext = computed(() => offset.value + items.value.length < total.value);
const firstIndex = computed(() => (items.value.length ? offset.value + 1 : 0));
const lastIndex = computed(() => offset.value + items.value.length);
const searching = computed(() => query.value.trim().length > 0);
// 开关里任何一路被关掉都提示一次:它们只是暂停,不是删除。
const paused = computed(() => {
  const s = status.value;
  if (!s) return [] as string[];
  const out: string[] = [];
  if (!s.write_enabled) out.push("自动写入(从对话里提取新记忆)已暂停");
  if (!s.search_enabled) out.push("回答时的记忆注入已暂停");
  if (!s.maintenance_enabled) out.push("记忆维护(合并 / 改写)已暂停");
  return out;
});

// ── 读 ──

async function load(opts: { keepPage?: boolean } = {}): Promise<void> {
  if (!opts.keepPage) offset.value = 0;
  loading.value = true;
  error.value = "";
  try {
    const res = await listMemory(query.value, PAGE_SIZE, offset.value);
    items.value = res.items;
    total.value = res.total;
    degraded.value = res.degraded;
    // 检索整体不可用(向量库挂 / MySQL 读不到):后端仍返回 200 但 error 有值 ——
    // 照实显示,不能把它当成「你没有记忆」。
    error.value = res.error;
    loaded.value = true;
  } catch (e) {
    // 未配库 / 功能关闭是 503,后端给的说明本身就是给人看的,直接采用
    error.value = errorText(e);
    items.value = [];
    total.value = 0;
    degraded.value = [];
  } finally {
    loading.value = false;
  }
}

async function loadStatus(): Promise<void> {
  statusError.value = "";
  try {
    status.value = await getMemoryStatus();
  } catch (e) {
    statusError.value = errorText(e);
  }
}

// 页面首屏 / KeepAlive 回来 / 手动刷新:一次把列表与状态都拿到。
async function refresh(opts: { keepPage?: boolean } = {}): Promise<void> {
  await Promise.all([load(opts), loadStatus()]);
}

function search(): void {
  const q = draft.value.trim();
  query.value = q;
  offset.value = 0;
  void load();
}

function clearSearch(): void {
  draft.value = "";
  query.value = "";
  offset.value = 0;
  void load();
}

function goPage(delta: number): void {
  const start = offset.value + delta * PAGE_SIZE;
  if (start < 0 || start >= total.value) return;
  offset.value = start;
  void load({ keepPage: true });
}

// ── 写 ──

// 手添一条记忆:成功后就地插到列表头部(不整页重拉,免得翻页位置跳走)。
async function add(): Promise<boolean> {
  const text = addText.value.trim();
  if (!text || adding.value) return false;
  adding.value = true;
  addError.value = "";
  try {
    const item = await addMemory(text);
    addText.value = "";
    items.value = [item, ...items.value];
    total.value += 1;
    if (status.value) status.value.items += 1;
    return true;
  } catch (e) {
    addError.value = errorText(e);
    return false;
  } finally {
    adding.value = false;
  }
}

function startEdit(item: MemoryItem): void {
  editingId.value = item.id;
  editDraft.value = item.text;
  editError.value = "";
}

function cancelEdit(): void {
  editingId.value = "";
  editDraft.value = "";
  editError.value = "";
}

// 保存改写:后端按版本号做 CAS,期间被别处改过 → 409(不覆盖),提示刷新后重试。
async function saveEdit(): Promise<void> {
  const id = editingId.value;
  const text = editDraft.value.trim();
  if (!id || !text || saving.value) return;
  saving.value = true;
  busyId.value = id;
  editError.value = "";
  try {
    const fresh = await editMemory(id, text);
    const at = items.value.findIndex((i) => i.id === id);
    if (at >= 0) items.value[at] = fresh;
    cancelEdit();
  } catch (e) {
    editError.value = errorText(e);
  } finally {
    saving.value = false;
    busyId.value = "";
  }
}

// 删除单条:两步走(先点「删除」、行内出现「确认删除」),不弹原生 confirm。
function askRemove(id: string): void {
  removingId.value = id;
}

function cancelRemove(): void {
  removingId.value = "";
}

async function remove(item: MemoryItem): Promise<void> {
  if (busyId.value) return;
  busyId.value = item.id;
  error.value = "";
  deleteNote.value = "";
  try {
    const res = await deleteMemory(item.id);
    items.value = items.value.filter((i) => i.id !== item.id);
    total.value = Math.max(0, total.value - 1);
    removingId.value = "";
    if (status.value) status.value.items = Math.max(0, status.value.items - 1);
    // 事实已经删掉(这条不会再被检索到);索引没清干净时照实说,并顺手刷一次状态,
    // 让「清理台账」那几个数字跟着动 —— 用户看得到后台还在收尾。
    if (res.cleanup !== "done") {
      deleteNote.value = "已删除。它的向量索引没能在这次清干净，后台会按退避继续重试"
        + (res.degraded.length ? `；未做到最好的地方：${res.degraded.join("；")}` : "");
      void loadStatus();
    } else {
      deleteNote.value = "已删除。这条不再被使用，对应的向量索引也已清掉（聊天记录不受影响）。";
    }
  } catch (e) {
    error.value = errorText(e);
  } finally {
    busyId.value = "";
  }
}

function dismissDeleteNote(): void {
  deleteNote.value = "";
}

// 彻底清除:后端按逐项结果如实汇报(含向量没清干净的情况),原样展示给用户。
async function clearAll(): Promise<boolean> {
  if (clearing.value) return false;
  clearing.value = true;
  clearError.value = "";
  try {
    clearResult.value = await clearMemory();
    items.value = [];
    total.value = 0;
    query.value = "";
    draft.value = "";
    offset.value = 0;
    cancelEdit();
    await loadStatus();
    return true;
  } catch (e) {
    clearError.value = errorText(e);
    return false;
  } finally {
    clearing.value = false;
  }
}

function dismissClearResult(): void {
  clearResult.value = null;
}

// 补索引:只动派生数据(向量),失败也不影响记忆本身 —— 结果照实报。
async function reindex(): Promise<void> {
  if (reindexing.value) return;
  reindexing.value = true;
  reindexNote.value = "";
  try {
    const r = await reindexMemory();
    const parts = [`本次取出 ${r.requested} 条,补写 ${r.indexed} 条`];
    if (r.payload_only) parts.push(`另有 ${r.payload_only} 条只更新了元数据(正文没变,不必重新向量化)`);
    if (r.deferred) parts.push(`仍有 ${r.deferred} 条待补`);
    if (r.error) parts.push(`失败原因:${r.error}`);
    reindexNote.value = parts.join(";");
    await Promise.all([load(), loadStatus()]);
  } catch (e) {
    reindexNote.value = errorText(e);
  } finally {
    reindexing.value = false;
  }
}

// 登出 / 换账号:清空本模块的全部状态(否则下一个账号会看到上一个账号的记忆)。
function reset(): void {
  items.value = [];
  total.value = 0;
  query.value = "";
  draft.value = "";
  offset.value = 0;
  status.value = null;
  statusError.value = "";
  loading.value = false;
  error.value = "";
  degraded.value = [];
  loaded.value = false;
  adding.value = false;
  addText.value = "";
  addError.value = "";
  editingId.value = "";
  editDraft.value = "";
  saving.value = false;
  editError.value = "";
  removingId.value = "";
  busyId.value = "";
  deleteNote.value = "";
  clearing.value = false;
  clearError.value = "";
  clearResult.value = null;
  reindexing.value = false;
  reindexNote.value = "";
}

export function useMemory() {
  return {
    items, total, query, draft, offset, status, statusError, loading, error, degraded, loaded,
    adding, addText, addError, editingId, editDraft, saving, editError, removingId, busyId,
    deleteNote, clearing, clearError, clearResult, reindexing, reindexNote,
    hasPrev, hasNext, firstIndex, lastIndex, searching, paused, pageSize: PAGE_SIZE,
    refresh, load, loadStatus, search, clearSearch, goPage, add, startEdit, cancelEdit, saveEdit,
    askRemove, cancelRemove, remove, dismissDeleteNote, clearAll, dismissClearResult, reindex, reset,
  };
}
