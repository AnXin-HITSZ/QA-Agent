// 待办清单的前端状态:全局一份,后端 Redis 为准。
// 模块级单例:TodoDrawer(抽屉内容)与 ChatView(工具键上的未完成徽标)共享同一份
// items / openCount，改一处两处齐更新，免去透传 props。
import { computed, ref } from "vue";

import {
  createTodo,
  deleteTodo,
  listTodos,
  updateTodo,
  type Todo,
  type TodoCreate,
} from "../api";

// 全部待办（未完成在前，见 sortTodos）。
const items = ref<Todo[]>([]);
// 后端待办存储（Redis）是否启用；false = 未配置。
const enabled = ref(false);
// 已启用但本次读取失败：列表恒空、抽屉提示重试。
const degraded = ref(false);
// 拉取 / 写入进行中，用于禁用按钮、显示态。
const loading = ref(false);
// 最近一次写操作的错误文案（新建 / 勾选 / 删除失败）；成功即清空。
const error = ref("");
// 只在首次需要时拉一次（抽屉首挂 / 徽标首挂），避免重复请求。
let loadedOnce = false;

// 未完成条数，供工具键徽标显示。
const openCount = computed(() => items.value.filter((t) => !t.done).length);

// 与后端 _sorted 同序：未完成在前 → 截止升序（无截止排后）→ 创建倒序（新在前）。
// 本地也排一遍，勾选 / 新建后无需重拉即可就地归位。
function sortTodos(list: Todo[]): Todo[] {
  return [...list].sort((a, b) => {
    if (a.done !== b.done) return a.done ? 1 : -1;
    const ad = a.due_date;
    const bd = b.due_date;
    if ((ad === null) !== (bd === null)) return ad === null ? 1 : -1;
    if (ad && bd && ad !== bd) return ad < bd ? -1 : 1;
    return a.created_at < b.created_at ? 1 : -1;
  });
}

// 拉全部待办。失败不谎称未启用，交由 listTodos 标 degraded → 抽屉给重试。
async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const res = await listTodos();
    enabled.value = res.enabled;
    degraded.value = res.degraded;
    items.value = res.items;
    loadedOnce = true;
  } finally {
    loading.value = false;
  }
}

// 首次需要时才拉（抽屉 / 徽标各自 onMounted 调用，只有第一处真正发请求）。
async function ensureLoaded(): Promise<void> {
  if (!loadedOnce) await load();
}

// 新建一条。成功返回 true 并就地插入排序；失败置 error 返回 false（表单据此不清空）。
async function add(body: TodoCreate): Promise<boolean> {
  error.value = "";
  try {
    const todo = await createTodo(body);
    items.value = sortTodos([...items.value, todo]);
    return true;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
    return false;
  }
}

// 勾选 / 取消完成：乐观翻转 + 就地重排，失败回滚。
async function toggle(id: string): Promise<void> {
  const t = items.value.find((x) => x.id === id);
  if (!t) return;
  const prev = t.done;
  error.value = "";
  t.done = !t.done;
  items.value = sortTodos([...items.value]);
  try {
    const updated = await updateTodo(id, { done: t.done });
    Object.assign(t, updated);
    items.value = sortTodos([...items.value]);
  } catch (e) {
    t.done = prev; // 回滚
    items.value = sortTodos([...items.value]);
    error.value = e instanceof Error ? e.message : String(e);
  }
}

// 删除：乐观移除，失败整列表回滚。
async function remove(id: string): Promise<void> {
  const snapshot = items.value;
  error.value = "";
  items.value = items.value.filter((x) => x.id !== id);
  try {
    await deleteTodo(id);
  } catch (e) {
    items.value = snapshot;
    error.value = e instanceof Error ? e.message : String(e);
  }
}

export function useTodos() {
  return { items, enabled, degraded, loading, error, openCount, load, ensureLoaded, add, toggle, remove };
}
