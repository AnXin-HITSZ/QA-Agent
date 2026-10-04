// 知识库浏览与管理:逐层浏览 + 新建分类 / 上传(上传后建索引任务)/ 删除 / 索引任务 / 版本回退。
// 模块级单例:知识库视图、工具条与索引任务面板共享同一份状态,不层层透传。
//
// 所有索引都走同一个任务入口 POST /index-jobs(202 + 轮询):上传后的自动索引与单文件重试
// 都是 kind=keys 的任务,进度由任务明细回填到上传行,后端单工作者天然互斥。
//
// 提取方式(OCR 接入,见 docs/OCR接入技术方案.md §8)是模块级的一份共享设置:
// 上传索引、上传行重试、任务失败重试都用它;首次索引前必须显式选择("" = 未选择),
// 不隐式替用户选一种方式 —— native_only 不产生付费调用,其余四种会调用云端 OCR。
// (整库 / 分类树的批量重建入口已从界面移除:一次重建可能跨多种提取方式,一个手动
//  选择器表达不了;计划改为给每个索引任务记提取方式,重建时按任务原方式整批复用。)
import { computed, ref, watch } from "vue";

import {
  ApiError,
  createFolder as apiCreateFolder,
  createIndexJob,
  deleteFile as apiDeleteFile,
  deleteFolder as apiDeleteFolder,
  getCurrentIndexJob,
  getIndexJob,
  getIndexJobFiles,
  getIndexManifest,
  getTree,
  indexedKeys,
  rollbackIndex as apiRollbackIndex,
  uploadFiles,
  type ExtractionMode,
  type IndexJobFileRow,
  type IndexJobScope,
  type IndexJobStatus,
  type IndexManifestInfo,
  type IndexOptions,
  type KnowledgeTree,
  type UploadResult,
} from "../api";

// ── 提取方式 ──

export interface ModeSpec {
  value: ExtractionMode;
  label: string;
  hint: string;
}

// 五种手动提取方式,与后端 ExtractionMode 枚举一一对应(不做自动分类 / 自动切换)。
export const EXTRACTION_MODES: readonly ModeSpec[] = [
  { value: "native_only", label: "不使用 OCR", hint: "只取原生文本层,不产生付费调用" },
  { value: "general", label: "通用文字识别(基础版)", hint: "整页识别为文字;单价低,复杂版面精度一般" },
  { value: "general_advanced", label: "通用文字识别(高精版)", hint: "整页识别为文字;复杂背景 / 倾斜 / 印章更稳,单价更高" },
  { value: "invoice", label: "发票识别", hint: "按发票结构识别,字段转成可检索文本" },
  { value: "payment_record", label: "付款详情识别", hint: "付款记录 / 转账详情截图" },
];

// 方式名 → 展示文案(任务明细、进度文案里复用)。
export function modeLabel(mode: string | null | undefined): string {
  return EXTRACTION_MODES.find((m) => m.value === mode)?.label ?? (mode || "未指定");
}

// "" = 尚未选择。首次索引前必须选;缺省不替用户决定。
const extractionMode = ref<ExtractionMode | "">("");
const mixedInvoice = ref(false); // 「混贴票据页」:仅发票识别可用
const refreshOcr = ref(false); // 忽略 OCR 缓存重新识别:native_only 无意义
const modeChosen = computed(() => extractionMode.value !== "");

// 切换方式时清掉不适用选项(与后端 422 规则一致:混贴仅限发票,刷新缓存不能配原生提取)。
watch(extractionMode, (m) => {
  if (m !== "invoice") mixedInvoice.value = false;
  if (m === "native_only") refreshOcr.value = false;
});

// 当前选项:所有索引调用(上传 / 重试 / 任务)共用这一份。
function currentOptions(): IndexOptions {
  const mode = extractionMode.value;
  return {
    extraction_mode: mode || "native_only",
    mixed_invoice: mode === "invoice" && mixedInvoice.value,
    refresh_ocr: mode !== "" && mode !== "native_only" && refreshOcr.value,
  };
}

// ── 分类树 / 列表状态 ──

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
  | "need_mode" // 已上传,但没选提取方式,尚未索引(选择方式后可重试)
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

// 上传 + 索引的进度行 + 整批是否进行中(索引进度由任务明细回填,见 watchedRows)。
const uploadRows = ref<UploadRow[]>([]);
const uploading = ref(false);

// ── 索引任务状态 ──

const job = ref<IndexJobStatus | null>(null);
const jobFiles = ref<IndexJobFileRow[]>([]);
const jobFilesTotal = ref(0);
// 创建任务的请求进行中(按钮防重复点)。
const jobStarting = ref(false);
// 任务失败 / 创建失败的提示(发布失败时这里说明「旧索引仍可用」)。
const jobError = ref("");
const manifest = ref<IndexManifestInfo | null>(null);
const jobRunning = computed(
  () => job.value?.status === "queued" || job.value?.status === "running",
);

// ── 分类树读取 ──

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

// ── 上传(上传完建一个 keys 索引任务,进度回填到行上)──

// 清空上传进度(关闭上传面板 / 开始新一批前)。
function clearUploads(): void {
  uploadRows.value = [];
}

// 等任务结束时回填结果的上传行(key → 行)。上传批次和行内重试各自登记。
const watchedRows = new Map<string, UploadRow>();

// 上传一批散文件到当前节点;成功入库的建一个 keys 任务去做索引(用当前提取方式)。
// 没选提取方式时仍然上传(原件先入库),索引留到选好方式后重试 —— 不替用户隐式选方式。
// 上传请求一返回就释放 uploading:索引进度由任务面板与行状态继续反映。
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

  // 让新文件立即出现在列表里(索引完成后徽标再刷新)。
  await loadTree(prefix.value);
  uploading.value = false;

  const rows = uploadRows.value.filter((r) => r.phase === "indexing");
  if (!rows.length) return;
  if (!modeChosen.value) {
    for (const r of rows) r.phase = "need_mode"; // 选好提取方式后可在行内重试 / 一键继续
    return;
  }
  await startRowJob(rows);
}

// 为这批上传行起一个索引任务(显式 key 清单),结果在任务结束时回填到行上。
async function startRowJob(rows: UploadRow[]): Promise<void> {
  if (!rows.length) return;
  const ok = await startJob({ kind: "keys", keys: rows.map((r) => r.key) }, rows);
  if (!ok) {  // 建任务失败(依赖未接通 / 已有任务在跑):行上给出原因,可稍后重试
    for (const r of rows) {
      if (r.phase === "indexing") {
        r.phase = "index_failed";
        r.reason = jobError.value || "未能创建索引任务";
      }
    }
  }
}

// 重试某个上传行(索引失败 / 尚未选方式);用当前提取方式。
async function retryUploadRow(row: UploadRow): Promise<void> {
  if (uploading.value || jobRunning.value || jobStarting.value) return;
  if (!modeChosen.value) {
    row.phase = "need_mode";
    row.reason = undefined;
    return;
  }
  row.phase = "indexing";
  row.reason = undefined;
  await startRowJob([row]);
}

// 把当前列表里仍是 need_mode 的行一起补索引(选好方式后一键继续)。
async function indexPendingUploads(): Promise<void> {
  if (uploading.value || jobRunning.value || !modeChosen.value) return;
  const rows = uploadRows.value.filter((r) => r.phase === "need_mode");
  for (const r of rows) {
    r.phase = "indexing";
    r.reason = undefined;
  }
  await startRowJob(rows);
}

// ── 写操作(均对齐后端 /knowledge 与 /admin/knowledge 契约)──

// 在当前节点下新建一个空分类(单层名)。失败抛出,交调用方就地提示。
async function makeFolder(name: string): Promise<void> {
  await apiCreateFolder(prefix.value, name);
  await loadTree(prefix.value);
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

// ── 索引任务(202 + 轮询)──

let pollTimer: ReturnType<typeof setTimeout> | null = null;

function stopPoll(): void {
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

// 追加拉取任务新增的文件明细(offset = 已有行数)。
async function fetchNewDetails(jobId: string): Promise<void> {
  const page = await getIndexJobFiles(jobId, jobFiles.value.length, 200);
  if (jobId !== job.value?.job_id) return; // 期间换了任务:丢弃过期结果
  jobFilesTotal.value = page.total;
  if (page.items.length) jobFiles.value = [...jobFiles.value, ...page.items];
}

// 任务结束后把上传行按最终结果结算。
// 明细里的 indexed 只代表"写进了候选版本",发布失败会整体作废 —— 所以不在明细到达时
// 就点亮,一律等任务结束;失败时统一标失败并说明本次未发布、旧索引仍可用。
function resolveWatchedRows(published: boolean, message: string): void {
  if (!watchedRows.size) return;
  const byKey = new Map(jobFiles.value.map((d) => [d.key, d]));
  for (const row of watchedRows.values()) {
    const d = byKey.get(row.key);
    if (!published) {
      row.phase = "index_failed";
      row.reason = message || "本次未发布,旧索引继续可用";
    } else if (d?.status === "indexed") {
      row.phase = "indexed";
      row.chunks = d.chunks;
      indexedSet.value = new Set(indexedSet.value).add(row.key); // 徽标即时点亮
      indexReady.value = true;
    } else {
      row.phase = "index_failed";
      row.reason = d?.reason || "未建立索引";
    }
  }
  watchedRows.clear();
}

async function pollOnce(jobId: string): Promise<void> {
  let finished = false;
  try {
    const status = await getIndexJob(jobId);
    if (jobId !== job.value?.job_id) return;
    job.value = status;
    finished = status.status === "published" || status.status === "failed";
    if (finished) {
      jobError.value =
        status.status === "failed" ? (status.error ?? status.summary?.message ?? "任务未发布") : "";
      void refreshManifest();
      void loadTree(prefix.value); // 发布后刷新「已索引」徽标
    }
  } catch (e) {
    // 单次轮询失败(网络抖动 / 后端重启)不终止:下一拍继续,避免面板停在中途。
    if (jobId !== job.value?.job_id) return;
    jobError.value = e instanceof Error ? e.message : String(e);
  }
  try {
    await fetchNewDetails(jobId); // 结束时先补齐明细,再按它结算上传行
  } catch {
    // 明细稍后再拉;状态显示不依赖它
  }
  if (finished) {
    resolveWatchedRows(job.value?.status === "published", jobError.value);
  } else if (jobId === job.value?.job_id) {
    pollTimer = setTimeout(() => void pollOnce(jobId), 1000);
  }
}

// 开始一个索引任务(整库 / 子树 / 显式文件清单)。失败只写 jobError,不抛,返回是否建成。
// watch: 用这批上传行接住任务结果(上传后自动索引 / 行内重试),与任务一一对应。
async function startJob(scope: IndexJobScope, watch?: UploadRow[]): Promise<boolean> {
  if (jobRunning.value || jobStarting.value) {
    jobError.value = "已有索引任务在进行中,请等它结束后再试。";
    return false;
  }
  jobError.value = "";
  if (!modeChosen.value) {
    jobError.value = "请先选择一种提取方式,再开始索引。";
    return false;
  }
  jobStarting.value = true;
  try {
    const created = await createIndexJob(scope, currentOptions());
    jobFiles.value = []; // 换任务:明细从头开始拉
    jobFilesTotal.value = 0;
    watchedRows.clear(); // 上一个任务的待结算行不再属于本任务
    for (const row of watch ?? []) {
      if (row.phase === "indexing") watchedRows.set(row.key, row);
    }
    job.value = created;
    stopPoll();
    pollTimer = setTimeout(() => void pollOnce(created.job_id), 400);
    return true;
  } catch (e) {
    jobError.value = e instanceof Error ? e.message : String(e);
    return false;
  } finally {
    jobStarting.value = false;
  }
}

function jobIdOf(j: IndexJobStatus | null): string | null {
  return j?.job_id ?? null;
}

// 重试任务明细里的单个失败文件:起一个只含它的 keys 任务(结果在任务明细里看)。
async function retryJobFile(row: IndexJobFileRow): Promise<void> {
  if (!modeChosen.value) {
    jobError.value = "请先选择一种提取方式,再重试。";
    return;
  }
  await startJob({ kind: "keys", keys: [row.key] });
}

// 进入知识库视图时恢复:进行中的任务继续轮询,最近一个任务展示其结束结果。
async function resumeJob(): Promise<void> {
  try {
    const cur = await getCurrentIndexJob();
    if (!cur) return;
    if (jobIdOf(job.value) === cur.job_id && jobRunning.value) return; // 已在轮询
    job.value = cur;
    jobFiles.value = [];
    jobFilesTotal.value = 0;
    stopPoll();
    if (cur.status === "queued" || cur.status === "running") {
      pollTimer = setTimeout(() => void pollOnce(cur.job_id), 300);
    } else {
      await fetchNewDetails(cur.job_id).catch(() => undefined);
      jobError.value = cur.status === "failed" ? (cur.error ?? "") : "";
    }
  } catch {
    // 后端未接通:静默,不影响浏览
  }
}

// 当前生效版本与可回退版本(面板展示用)。失败保持旧值。
async function refreshManifest(): Promise<void> {
  try {
    manifest.value = await getIndexManifest();
  } catch {
    // 未接通:不显版本信息
  }
}

// 回退到上一版本(只切指针)。有任务在跑后端会拒绝(409);失败抛出交调用方提示。
// 直接吃 POST 的响应(与 GET 同构)——回退是终点,不用再补一次读取,提示里的版本号也不会滞后。
async function rollbackToPrevious(): Promise<void> {
  manifest.value = await apiRollbackIndex();
  await loadTree(prefix.value);
}

export function useKnowledge() {
  return {
    // 提取方式(模块级共享设置)
    EXTRACTION_MODES,
    extractionMode,
    mixedInvoice,
    refreshOcr,
    modeChosen,
    modeLabel,
    // 分类树 / 列表
    prefix,
    tree,
    loading,
    error,
    storageEnabled,
    indexReady,
    // 上传
    uploadRows,
    uploading,
    clearUploads,
    uploadAndIndex,
    retryUploadRow,
    indexPendingUploads,
    // 写操作
    makeFolder,
    removeFile,
    removeFolder,
    // 索引任务
    job,
    jobFiles,
    jobFilesTotal,
    jobStarting,
    jobError,
    jobRunning,
    manifest,
    startJob,
    retryJobFile,
    resumeJob,
    refreshManifest,
    rollbackToPrevious,
    // 浏览导航
    loadTree,
    enter,
    up,
    goto,
    refresh,
    isIndexed,
  };
}
