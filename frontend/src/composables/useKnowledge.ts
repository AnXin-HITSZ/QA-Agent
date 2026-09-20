// 知识库浏览与管理:逐层浏览 + 新建分类 / 上传(上传即索引)/ 删除 / 维护重建。
// 模块级单例:知识库视图与工具条 / 上传状态 / 重建条共享同一份状态,不层层透传。
import { ref } from "vue";

import {
  ApiError,
  createFolder as apiCreateFolder,
  deleteFile as apiDeleteFile,
  deleteFolder as apiDeleteFolder,
  getTree,
  indexFile,
  indexedKeys,
  reindexKnowledge,
  uploadFiles,
  type KnowledgeTree,
  type ReindexResult,
  type UploadResult,
} from "../api";

// ── 模块级单例状态 ──
// 当前节点前缀:归一化为 "" 或以 "/" 结尾,与后端 KnowledgeTree.prefix 一致。
const prefix = ref("");
const tree = ref<KnowledgeTree | null>(null);
const loading = ref(false);
const error = ref("");
// OSS 是否接通:后端未配 OSS 返回 503 → false,视图据此显示「存储未接通」而非报错。
const storageEnabled = ref(true);
// 已索引原件 key 集合(当前节点直属文件)+ 索引状态是否可用。
// indexReady=false(Qdrant 未接通)时不显徽标,避免把「未索引」和「查不到」混为一谈。
const indexedSet = ref<Set<string>>(new Set());
const indexReady = ref(false);

// 一次上传这批文件的逐个进度阶段。
export type UploadPhase =
  | "uploading" // 正在传进 OSS
  | "indexing" // 已入库,正在建索引
  | "indexed" // 索引完成
  | "index_failed" // 索引失败(reason 说明)
  | "skipped_exists" // 同名已存在,未覆盖
  | "rejected" // 文件名非法,未接收
  | "upload_error"; // 上传阶段就失败

export interface UploadRow {
  name: string;
  key: string;
  phase: UploadPhase;
  chunks?: number;
  reason?: string;
}

// 上传 + 逐个索引的进度行 + 整批是否进行中。
const uploadRows = ref<UploadRow[]>([]);
const uploading = ref(false);
// 维护重建进行中 + 最近一次结果(供重建条展示统计)。
const reindexing = ref(false);
const reindexResult = ref<ReindexResult | null>(null);

// 载入某节点:先取树(OSS),再单独取已索引 key(Qdrant,失败则降级不显徽标)。
async function loadTree(p: string = prefix.value): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const t = await getTree(p);
    tree.value = t;
    prefix.value = t.prefix; // 用后端归一化后的前缀,导航才不漂移
    storageEnabled.value = true;
  } catch (e) {
    if (e instanceof ApiError && e.status === 503) {
      storageEnabled.value = false; // 存储未接通:走空态,不当错误
      tree.value = null;
    } else {
      error.value = e instanceof Error ? e.message : String(e);
    }
    loading.value = false;
    return;
  }
  // 索引徽标单独取:Qdrant 未接通不该阻断浏览,失败则静默降级(不显徽标)。
  try {
    const r = await indexedKeys(prefix.value);
    indexedSet.value = new Set(r.keys);
    indexReady.value = true;
  } catch {
    indexedSet.value = new Set();
    indexReady.value = false;
  }
  loading.value = false;
}

// 进入某子节点(folder 为 list_children 给的单层名)。
function enter(folder: string): void {
  void loadTree(prefix.value + folder + "/");
}

// 回上一层(根节点无操作)。
function up(): void {
  const p = prefix.value;
  if (!p) return;
  const trimmed = p.replace(/\/$/, "");
  const idx = trimmed.lastIndexOf("/");
  void loadTree(idx >= 0 ? trimmed.slice(0, idx + 1) : "");
}

// 跳到指定前缀(面包屑点击;"" = 根)。
function goto(p: string): void {
  void loadTree(p);
}

// 重新载入当前节点(增删改后刷新)。
function refresh(): void {
  void loadTree(prefix.value);
}

// 某文件是否已建立索引(索引状态不可用时一律 false,由视图据 indexReady 决定是否显徽标)。
function isIndexed(key: string): boolean {
  return indexReady.value && indexedSet.value.has(key);
}

// ── 写操作(均对齐后端 /knowledge 与 /admin/knowledge 契约)──

// 在当前节点下新建一个空分类(单层名)。失败抛出,交调用方就地提示。
async function makeFolder(name: string): Promise<void> {
  await apiCreateFolder(prefix.value, name);
  await loadTree(prefix.value);
}

// 清空上传进度(关闭上传面板 / 开始新一批前)。
function clearUploads(): void {
  uploadRows.value = [];
}

// 上传一批散文件到当前节点;成功入库的再逐个建索引。
// 串行索引(非并发):契合 embeddings ≤10/请求限流,且反馈能精确定位到某个文件。
async function uploadAndIndex(files: File[]): Promise<void> {
  if (!files.length || uploading.value) return;
  uploading.value = true;
  // 先铺一批 uploading 行给即时反馈(上传是整批一次调用,回来才有逐文件结果)。
  uploadRows.value = files.map((f): UploadRow => ({ name: f.name, key: "", phase: "uploading" }));

  let res: UploadResult;
  try {
    res = await uploadFiles(prefix.value, files);
  } catch (e) {
    const reason = e instanceof Error ? e.message : String(e);
    uploadRows.value = uploadRows.value.map((r): UploadRow => ({ ...r, phase: "upload_error", reason }));
    uploading.value = false;
    return;
  }

  // 用后端逐文件结果重建进度行:uploaded → 待索引;其余保持后端判定。
  uploadRows.value = res.items.map(
    (it): UploadRow => ({
      name: it.name,
      key: it.key,
      phase:
        it.status === "uploaded"
          ? "indexing"
          : it.status === "skipped_exists"
            ? "skipped_exists"
            : "rejected",
    }),
  );

  // 让新文件立即出现在列表里(索引徽标随后逐个补上)。
  await loadTree(prefix.value);

  // 串行逐个索引;遇依赖未接通(503)则停手,把余下标为失败,免连打 N 次必失败的请求。
  for (const row of uploadRows.value) {
    if (row.phase !== "indexing") continue;
    try {
      const r = await indexFile(row.key);
      if (r.indexed) {
        row.phase = "indexed";
        row.chunks = r.chunks;
        indexedSet.value = new Set(indexedSet.value).add(row.key); // 徽标即时点亮
        indexReady.value = true;
      } else {
        row.phase = "index_failed";
        row.reason = r.reason ?? undefined;
      }
    } catch (e) {
      row.phase = "index_failed";
      row.reason = e instanceof Error ? e.message : String(e);
      if (e instanceof ApiError && e.status === 503) {
        for (const rest of uploadRows.value) {
          if (rest.phase === "indexing") {
            rest.phase = "index_failed";
            rest.reason = row.reason;
          }
        }
        break;
      }
    }
  }
  uploading.value = false;
}

// 删单个文件(后端连带 best-effort 清向量),然后刷新当前节点。失败抛出。
async function removeFile(key: string): Promise<void> {
  await apiDeleteFile(key);
  await loadTree(prefix.value);
}

// 删整个子分类(递归清该前缀下全部对象),然后刷新当前节点。返回删除对象数。失败抛出。
async function removeFolder(p: string): Promise<number> {
  const r = await apiDeleteFolder(p);
  await loadTree(prefix.value);
  return r.deleted;
}

// 维护兜底:全量重建索引。留空 = 整库。注意后端 v1 会先清空整个 collection 再重建。
async function runReindex(scope = ""): Promise<void> {
  if (reindexing.value) return;
  reindexing.value = true;
  reindexResult.value = null;
  try {
    reindexResult.value = await reindexKnowledge(scope);
    await loadTree(prefix.value); // 重建后刷新徽标
  } finally {
    reindexing.value = false;
  }
}

export function useKnowledge() {
  return {
    prefix,
    tree,
    loading,
    error,
    storageEnabled,
    indexReady,
    uploadRows,
    uploading,
    reindexing,
    reindexResult,
    loadTree,
    enter,
    up,
    goto,
    refresh,
    isIndexed,
    makeFolder,
    clearUploads,
    uploadAndIndex,
    removeFile,
    removeFolder,
    runReindex,
  };
}
