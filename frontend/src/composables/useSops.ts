// SOP 流程:列表浏览 + 详情查看 + 新建 / 编辑 / 删除。
// 模块级单例:SopView 与其列表 / 详情 / 编辑三态子视图共享同一份状态与视图模式,不层层透传。
import { ref } from "vue";

import {
  ApiError,
  createSop,
  deleteSop,
  getSop,
  listSops,
  updateSop,
  type SopDetail,
  type SopSummary,
  type SopWrite,
} from "../api";

// 三态:列表 / 详情(只读)/ 编辑(新建或改)。
export type SopMode = "list" | "detail" | "editor";

// ── 模块级单例状态 ──
const list = ref<SopSummary[]>([]);
const loading = ref(false);
const error = ref("");
// OSS 是否接通:后端未配 OSS 返回 503 → false,视图据此显示「存储未接通」而非报错。
const storageEnabled = ref(true);
// 是否已成功载入过一次:供 SopView 首屏只自动载一次(单例切回时保留列表,不重载)。
const loaded = ref(false);

// 当前视图模式 + 详情 / 编辑载入的完整篇(含正文)。
const mode = ref<SopMode>("list");
const current = ref<SopDetail | null>(null);
const editingNew = ref(false); // 编辑器是否处于「新建」
const detailLoading = ref(false);
const detailError = ref("");

// 保存进行中 + 保存错误(编辑器就地提示)。
const saving = ref(false);
const saveError = ref("");

function msg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// 载入 SOP 列表。503 → 存储未接通(走空态,不当错误)。
async function loadList(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    list.value = await listSops();
    storageEnabled.value = true;
  } catch (e) {
    if (e instanceof ApiError && e.status === 503) {
      storageEnabled.value = false;
      list.value = [];
    } else {
      error.value = msg(e);
    }
  } finally {
    loaded.value = true;
    loading.value = false;
  }
}

function showList(): void {
  mode.value = "list";
}

// 打开某篇详情(拉取完整正文)。
async function openDetail(id: string): Promise<void> {
  mode.value = "detail";
  current.value = null;
  detailError.value = "";
  detailLoading.value = true;
  try {
    current.value = await getSop(id);
  } catch (e) {
    detailError.value = msg(e);
  } finally {
    detailLoading.value = false;
  }
}

// 打开编辑器:不传 id = 新建(空表单);传 id = 编辑(先拉取完整正文,表单据 current 填充)。
async function openEditor(id?: string): Promise<void> {
  saveError.value = "";
  mode.value = "editor";
  if (!id) {
    editingNew.value = true;
    current.value = null;
    detailError.value = "";
    detailLoading.value = false;
    return;
  }
  editingNew.value = false;
  current.value = null;
  detailError.value = "";
  detailLoading.value = true;
  try {
    current.value = await getSop(id);
  } catch (e) {
    detailError.value = msg(e);
  } finally {
    detailLoading.value = false;
  }
}

// 保存(新建 POST / 更新 PUT)。成功 → 刷新列表并跳到详情;失败 → saveError 就地提示,返回 false。
async function save(payload: SopWrite): Promise<boolean> {
  if (saving.value) return false;
  saving.value = true;
  saveError.value = "";
  try {
    const saved = editingNew.value ? await createSop(payload) : await updateSop(payload.id, payload);
    current.value = saved;
    await loadList();
    mode.value = "detail"; // 保存后回到详情,直接看落盘结果
    return true;
  } catch (e) {
    saveError.value = msg(e);
    return false;
  } finally {
    saving.value = false;
  }
}

// 删除某篇(失败抛出,交调用方就地提示);成功则清 current 并刷新列表。
async function remove(id: string): Promise<void> {
  await deleteSop(id);
  if (current.value?.id === id) current.value = null;
  await loadList();
}

export function useSops() {
  return {
    list,
    loading,
    error,
    storageEnabled,
    loaded,
    mode,
    current,
    editingNew,
    detailLoading,
    detailError,
    saving,
    saveError,
    loadList,
    showList,
    openDetail,
    openEditor,
    save,
    remove,
  };
}
