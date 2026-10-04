// 后端对话接口的类型与调用:非流式 /api/v1/chat + 流式 /api/v1/chat/stream。

export interface ChatRequest {
  message: string;
  thread_id?: string | null;
}

// 一条知识库引用来源:命中原件的元信息 + 短时效签名下载 URL(OSS 未配置时为 null)。
export interface Source {
  oss_key: string;
  source: string | null; // 文件名(展示名)
  category: string | null; // 所属分类前缀
  score: number | null; // 与问题的相关度(余弦相似度)
  url: string | null; // 15 分钟有效的签名下载 URL;为 null 时不可点
}

export interface SopImageReference {
  skill_id: string;
  sop_name: string;
  image_id: string;
  oss_key: string;
  alt: string;
  url: string;
}

export interface ChatResponse {
  images: SopImageReference[];
  skill: string | null; // 命中的 Skill id;null 表示走通用问答
  content: string;
  thread_id: string; // 本次会话线程 ID;续接记忆时回传
  sources: Source[]; // 本次回答引用的知识库来源(带签名 URL);无则为空
}

// 流式事件回调。后端 SSE:token/tool_call/tool_result 均带 step(agent 轮次号),done 附 skill + sources。
export interface StreamHandlers {
  onMeta?: (threadId: string) => void;
  onToolCall?: (name: string, args: Record<string, unknown>, step: number) => void;
  onToolResult?: (name: string | null, step: number) => void;
  onToken?: (content: string, step: number) => void;
  onDone?: (skill: string | null, sources: Source[], images: SopImageReference[]) => void;
}

export async function chat(message: string, threadId?: string | null): Promise<ChatResponse> {
  let res: Response;
  try {
    res = await fetch("/api/v1/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, thread_id: threadId ?? null } satisfies ChatRequest),
    });
  } catch {
    throw new Error("暂时连不上服务,请稍后重试。");
  }
  if (!res.ok) {
    throw new Error(`后端返回错误(HTTP ${res.status})。稍后重试,或查看后端日志。`);
  }
  return (await res.json()) as ChatResponse;
}

// 用户点「停止」造成的中断:与失败区分开,调用方据此不弹错误提示。
export class StreamAborted extends Error {
  constructor() {
    super("已停止生成");
    this.name = "StreamAborted";
  }
}

// EventSource 不支持 POST,这里用 fetch + ReadableStream 手动解析 SSE 帧(无新依赖)。
// signal 供「停止」中断;后端中途出错会补发 error 事件。两者之外,流必须以 done 收尾 ——
// 少了 done 就是被静默截断,这里抛错,不让半截答案冒充完整回答。
export async function chatStream(
  message: string,
  handlers: StreamHandlers,
  threadId?: string | null,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/v1/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, thread_id: threadId ?? null } satisfies ChatRequest),
      signal,
    });
  } catch {
    if (signal?.aborted) throw new StreamAborted();
    throw new Error("暂时连不上服务,请稍后重试。");
  }
  if (!res.ok || !res.body) {
    throw new Error(`后端返回错误(HTTP ${res.status})。稍后重试,或查看后端日志。`);
  }

  let doneSeen = false;
  let streamError = "";

  const dispatch = (frame: string): void => {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of frame.split(/\r?\n/)) {
      if (!line || line.startsWith(":")) continue; // 空行 / 注释(含 keep-alive ping)
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    }
    if (!dataLines.length) return;
    let payload: Record<string, unknown> | null = null;
    try {
      payload = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
    } catch {
      payload = null; // 兼容非 JSON 的收尾标记
    }
    const step = Number(payload?.step ?? 0); // agent 轮次号,据此把事件归入对应段
    switch (event) {
      case "meta":
        handlers.onMeta?.(String(payload?.thread_id ?? ""));
        break;
      case "tool_call":
        handlers.onToolCall?.(
          String(payload?.name ?? ""),
          (payload?.args as Record<string, unknown>) ?? {},
          step,
 );
        break;
      case "tool_result":
        handlers.onToolResult?.((payload?.name as string) ?? null, step);
        break;
      case "token": {
        const content = payload?.content;
        if (typeof content === "string" && content) handlers.onToken?.(content, step);
        break;
      }
      case "done":
        doneSeen = true;
        handlers.onDone?.(
          (payload?.skill as string) ?? null,
          (payload?.sources as Source[]) ?? [],
          (payload?.images as SopImageReference[]) ?? [],
        );
        break;
      case "error":
        streamError = String(payload?.message ?? "") || "回答中断,请重试。";
        break;
    }
  };

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const sep = /\r?\n\r?\n/;
  let buf = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let m: RegExpExecArray | null;
      while ((m = sep.exec(buf)) !== null) {
        const frame = buf.slice(0, m.index);
        buf = buf.slice(m.index + m[0].length);
        if (frame.trim()) dispatch(frame);
      }
      if (streamError) {
        await reader.cancel(); // 后端已明说失败,不必再等后续帧
        break;
      }
    }
    if (!streamError && buf.trim()) dispatch(buf); // flush 尾帧
  } catch (e) {
    if (signal?.aborted || (e instanceof DOMException && e.name === "AbortError")) {
      throw new StreamAborted();
    }
    throw new Error("回答未完成:与后端的连接在收尾前断开,请重试。");
  }

  if (streamError) throw new Error(streamError);
  if (!doneSeen) throw new Error("回答未完成:与后端的连接在收尾前断开,请重试。");
}

// ── 历史对话:后端以 Redis 为准,扫描 / 读取 / 删除会话 ──

// 搜索命中说明:命中的是提问还是回答、命中处的上下文片段、这通对话里命中几条。
export interface ConversationMatch {
  role: "user" | "assistant";
  snippet: string; // 命中处上下文;被截断的一端带 …
  count: number;
}

export interface ConversationSummary {
  thread_id: string;
  title: string;
  message_count: number;
  updated_at: string | null; // 最新 checkpoint 的 ISO 时间;null = 无
  match?: ConversationMatch | null; // 带 q 搜索时才有;未搜索为 null
}

export interface ConversationList {
  enabled: boolean; // 后端 Redis 跨轮记忆是否配置启用;false = 未配置(单轮模式)
  degraded: boolean; // 已启用但本次读取失败(超时/Redis 错误);true 时列表恒空、提示重试
  items: ConversationSummary[];
}

export interface ConversationMessage {
  images?: SopImageReference[];
  role: "user" | "assistant";
  content: string;
}

export interface ConversationDetail {
  thread_id: string;
  messages: ConversationMessage[];
}

// 列出历史会话(最近活跃在前);带 q 时由后端做内容检索,只返回命中的对话并附 match。
// 请求失败(后端不可用 / 非 2xx)不谎称"未启用",而是标记 degraded=true → 前端提示可重试。
export async function listConversations(q = ""): Promise<ConversationList> {
  const query = q.trim();
  const url = query
    ? `/api/v1/conversations?q=${encodeURIComponent(query)}`
    : "/api/v1/conversations";
  try {
    const res = await fetch(url);
    if (!res.ok) return { enabled: false, degraded: true, items: [] };
    const data = (await res.json()) as Partial<ConversationList>;
    return {
      enabled: data.enabled ?? false,
      degraded: data.degraded ?? false,
      items: data.items ?? [],
    };
  } catch {
    return { enabled: false, degraded: true, items: [] };
  }
}

// 读取一通历史会话的 Q&A 文本,用于回放。
export async function getConversation(threadId: string): Promise<ConversationDetail> {
  const res = await fetch(`/api/v1/conversations/${encodeURIComponent(threadId)}`);
  if (!res.ok) {
    throw new Error(`打开对话失败(HTTP ${res.status})。可能已被删除,或后端不可用。`);
  }
  return (await res.json()) as ConversationDetail;
}

// 删除一通历史会话,连带清掉它在 Redis 的记忆。
export async function deleteConversation(threadId: string): Promise<void> {
  const res = await fetch(`/api/v1/conversations/${encodeURIComponent(threadId)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    throw new Error(`删除对话失败(HTTP ${res.status})。稍后重试。`);
  }
}

// ── 知识库管理:分类树浏览 + 上传 / 删除 + 增量索引(对齐后端 /knowledge 与 /admin/knowledge)──

// 一个原件的元信息(列节点时用)。key 为知识库相对,唯一标识(删除 / 索引都用它)。
export interface KnowledgeFile {
  key: string;
  name: string; // 节点内的显示名(文件名)
  size: number; // 字节
  last_modified: number | null; // 最后修改时间(Unix 秒);目录标记无此值
}

// 某节点直接一层:子分类节点名 + 该节点下的文件。
export interface KnowledgeTree {
  prefix: string; // 当前节点(归一化:根为空串,其余以 / 结尾)
  folders: string[]; // 直接子分类节点名(仅一层,不含 /)
  files: KnowledgeFile[];
}

// 逐文件上传结果:uploaded 已传 / skipped_exists 同名已存在跳过 / rejected 文件名非法。
export interface UploadItem {
  name: string;
  key: string;
  status: "uploaded" | "skipped_exists" | "rejected";
}

export interface UploadResult {
  prefix: string;
  items: UploadItem[];
}

export interface DeleteFolderResult {
  prefix: string;
  deleted: number; // 删除的对象个数(含目录标记)
}

export interface IndexedKeysResult {
  prefix: string;
  keys: string[]; // 该节点下已建立索引的原件 key
}

// ── 提取方式与索引任务(OCR 接入,见 docs/OCR接入技术方案.md §8)──
//
// 提取方式由用户显式选择,不做自动分类 / 前缀路由 / 自动切换:
//   native_only  不使用 OCR,只取原生文本层(不产生付费调用)
//   general      通用文字识别
//   invoice      发票识别(可加 mixed_invoice = 「混贴票据页」)
//   payment_record 付款详情识别
export type ExtractionMode = "native_only" | "general" | "invoice" | "payment_record";

// 一次索引 / 重建的提取选项。非法组合(混贴非发票、刷新缓存配原生提取)后端返回 422。
export interface IndexOptions {
  extraction_mode: ExtractionMode;
  mixed_invoice?: boolean;
  refresh_ocr?: boolean;
}

// 任务范围:前缀子树与显式 key 清单互斥(两种都传后端返回 422)。
export interface IndexJobScope {
  kind: "prefix" | "keys";
  prefix?: string; // kind=prefix:知识库相对前缀;空 = 整库
  keys?: string[]; // kind=keys:显式文件清单
}

export interface JobPageStats {
  total: number; // 该文件总页数
  ok: number; // 成功提取的页
  blank: number; // 判定为空白页
  failed: number; // 识别失败 / 不完整的页
  cached: number; // 命中 OCR 缓存的页(未重复付费)
}

// 逐文件明细(任务进行中逐个追加,分页读取)。
export interface IndexJobFileRow {
  key: string;
  status: "indexed" | "skipped" | "failed";
  chunks: number;
  reason: string | null;
  method: string; // 实际用的提取方法:native / ocr(逗号分隔)
  ext: string;
  pages: JobPageStats;
  failed_pages: number[];
  warnings: string[];
}

export interface IndexJobFiles {
  job_id: string;
  total: number;
  offset: number;
  limit: number;
  items: IndexJobFileRow[];
}

export interface IndexJobSummary {
  index_version?: string;
  previous?: string;
  published?: boolean;
  total_files?: number;
  indexed_files?: number;
  skipped_files?: number;
  failed_files?: number;
  chunks?: number;
  vectors?: number;
  copied_out_of_scope?: number;
  replaced_in_scope?: number;
  pages?: JobPageStats;
  message?: string;
  skipped?: { key: string; reason: string }[];
  files_truncated?: boolean;
}

export interface IndexJobStatus {
  job_id: string;
  status: "queued" | "running" | "published" | "failed";
  scope: { kind?: string; prefix?: string; keys?: string[] };
  options: IndexOptions;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  progress: { done: number; total: number; current: string | null };
  published: boolean | null; // null = 尚未结束
  error: string | null; // 未发布时的原因
  summary: IndexJobSummary | null;
}

export interface IndexVersionInfo {
  name: string;
  role: string; // active / previous / staging / version / legacy
  points: number;
}

export interface IndexManifestInfo {
  active: string; // 当前生效的物理集合名
  previous: string | null; // 上一版本(回退目标)
  staging: string | null; // 正在构建的候选集合
  updated_at: string | null;
  versions: IndexVersionInfo[];
}

// 带 HTTP 状态码的错误,便于区分「存储/索引未接通」(503)与其它失败。
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// 优先用后端返回的 detail 文案(503 时是「未配置 …」的可读说明),否则用兜底。
async function detailOr(res: Response, fallback: string): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body?.detail === "string" && body.detail) return body.detail;
  } catch {
    // 无 JSON body,用兜底文案
  }
  return `${fallback}(HTTP ${res.status})。`;
}

async function kfetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    throw new ApiError(0, "暂时连不上服务,请稍后重试。");
  }
  if (!res.ok) throw new ApiError(res.status, await detailOr(res, "请求失败"));
  return (await res.json()) as T;
}

async function kfetchVoid(url: string, init?: RequestInit): Promise<void> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    throw new ApiError(0, "暂时连不上服务,请稍后重试。");
  }
  if (!res.ok) throw new ApiError(res.status, await detailOr(res, "操作失败"));
}

// 列某节点直接一层(子节点 + 文件)。OSS 未配置 → 抛 ApiError(status 503)。
export async function getTree(prefix = ""): Promise<KnowledgeTree> {
  return kfetchJson<KnowledgeTree>(`/api/v1/knowledge/tree?prefix=${encodeURIComponent(prefix)}`);
}

// 在父节点下手建一个空分类节点(单层名,不含 /)。
export async function createFolder(prefix: string, name: string): Promise<{ prefix: string }> {
  return kfetchJson<{ prefix: string }>(`/api/v1/knowledge/folder`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prefix, name }),
  });
}

// 把多个散文件平铺上传进目标节点(`prefix + 文件名`)。
export async function uploadFiles(prefix: string, files: File[]): Promise<UploadResult> {
  const form = new FormData();
  form.append("prefix", prefix);
  for (const f of files) form.append("files", f, f.name);
  return kfetchJson<UploadResult>(`/api/v1/knowledge/upload`, { method: "POST", body: form });
}

// 删单个文件(204 无 body)。
export async function deleteFile(key: string): Promise<void> {
  return kfetchVoid(`/api/v1/knowledge/object?key=${encodeURIComponent(key)}`, { method: "DELETE" });
}

// 删整个分类节点(递归清该前缀下全部对象);连带清向量由后端 best-effort 处理。
export async function deleteFolder(prefix: string): Promise<DeleteFolderResult> {
  return kfetchJson<DeleteFolderResult>(
    `/api/v1/knowledge/folder?prefix=${encodeURIComponent(prefix)}`,
    { method: "DELETE" },
  );
}

// 列出某节点下已建立索引的原件 key(供浏览时显示已/未索引徽标)。Qdrant 未配 → ApiError(503)。
export async function indexedKeys(prefix = ""): Promise<IndexedKeysResult> {
  return kfetchJson<IndexedKeysResult>(
    `/api/v1/admin/knowledge/indexed?prefix=${encodeURIComponent(prefix)}`,
  );
}

// ── 索引任务(唯一的索引入口:长任务异步执行,202 + 轮询)──

// 创建索引任务:立即返回 202 + job_id;已有任务在跑 → ApiError(409)。
// 提取方式必须显式传(后端强制),不隐式补默认值 —— 避免误产生付费 OCR 调用。
export async function createIndexJob(
  scope: IndexJobScope,
  options: IndexOptions,
): Promise<IndexJobStatus> {
  return kfetchJson<IndexJobStatus>(`/api/v1/admin/knowledge/index-jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, options }),
  });
}

// 查任务状态与进度。
export async function getIndexJob(jobId: string): Promise<IndexJobStatus> {
  return kfetchJson<IndexJobStatus>(`/api/v1/admin/knowledge/index-jobs/${encodeURIComponent(jobId)}`);
}

// 正在跑的任务(没有则返回最近一个任务;从未跑过 → null),进入页面时恢复进度显示。
export async function getCurrentIndexJob(): Promise<IndexJobStatus | null> {
  return kfetchJson<IndexJobStatus | null>(`/api/v1/admin/knowledge/index-jobs/current`);
}

// 分页读任务的文件明细(含页码统计、失败页、跳过原因)。
export async function getIndexJobFiles(
  jobId: string,
  offset = 0,
  limit = 100,
): Promise<IndexJobFiles> {
  return kfetchJson<IndexJobFiles>(
    `/api/v1/admin/knowledge/index-jobs/${encodeURIComponent(jobId)}/files?offset=${offset}&limit=${limit}`,
  );
}

// 当前生效的索引版本与本地版本集合(回退前确认用)。
export async function getIndexManifest(): Promise<IndexManifestInfo> {
  return kfetchJson<IndexManifestInfo>(`/api/v1/admin/knowledge/index-manifest`);
}

// 回退到上一版本(只切指针,不删集合)。有任务在跑 → ApiError(409)。
export async function rollbackIndex(): Promise<IndexManifestInfo> {
  return kfetchJson<IndexManifestInfo>(`/api/v1/admin/knowledge/index-manifest/rollback`, {
    method: "POST",
  });
}

// ── SOP 流程:查看 + 增删改查(对齐后端 /sops)──

// SOP 列表项(不含正文)。key 为桶内完整路径(sops/<id>.md);updated_at 为最后修改 Unix 秒。
export interface SopSummary {
  id: string;
  name: string;
  description: string;
  triggers: string[];
  key: string;
  updated_at: number | null;
}

// SOP 详情:在列表项基础上带 Markdown 正文。
export interface SopDetail extends SopSummary {
  body: string;
}

// 新建 / 更新 SOP 的请求体(id 不可改:新建时定名,更新须与路径一致)。
export interface SopWrite {
  id: string;
  name: string;
  description: string;
  triggers: string[];
  body: string;
}

// 上传 SOP 图片，返回正文可长期保存的同源访问路径。
export async function uploadSopImage(file: File): Promise<{ key: string; url: string }> {
  const form = new FormData();
  form.append("file", file, file.name);
  return kfetchJson<{ key: string; url: string }>("/api/v1/sops/images", {
    method: "POST",
    body: form,
  });
}

// 列出全部 SOP(不含正文,按 id 升序)。
export async function listSops(): Promise<SopSummary[]> {
  return kfetchJson<SopSummary[]>(`/api/v1/sops`);
}

// 读单篇 SOP 详情(含正文)。不存在 → ApiError(404)。
export async function getSop(id: string): Promise<SopDetail> {
  return kfetchJson<SopDetail>(`/api/v1/sops/${encodeURIComponent(id)}`);
}

// 新建 SOP。id 重复 → ApiError(409);id 非法 → ApiError(400)。
export async function createSop(body: SopWrite): Promise<SopDetail> {
  return kfetchJson<SopDetail>(`/api/v1/sops`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// 更新 SOP 内容(不可改 id)。不存在 → ApiError(404);路径 id 与内容 id 不一致 → 400。
export async function updateSop(id: string, body: SopWrite): Promise<SopDetail> {
  return kfetchJson<SopDetail>(`/api/v1/sops/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// 删除 SOP(204 无 body)。不存在 → ApiError(404)。
export async function deleteSop(id: string): Promise<void> {
  return kfetchVoid(`/api/v1/sops/${encodeURIComponent(id)}`, { method: "DELETE" });
}

// ── 待办清单:全局一份,后端 Redis 为准(与历史对话同一取向)。Agent 只读,增删改一律走这里 ──

export interface Todo {
  id: string;
  title: string;
  category: string; // 如「报销」「采购」;空串 = 未分类(展示层渲染为「未分类」)
  done: boolean;
  created_at: string; // ISO 8601
  due_date: string | null; // YYYY-MM-DD;null = 无截止
}

export interface TodoList {
  enabled: boolean; // 后端待办存储(Redis)是否启用;false = 未配置
  degraded: boolean; // 已启用但本次读取失败(超时/Redis 错误);true 时列表恒空、提示重试
  items: Todo[];
}

// 新建请求体(category / due_date 可省)。
export interface TodoCreate {
  title: string;
  category?: string;
  due_date?: string | null;
}

// 局部更新请求体:仅传入字段生效(勾选完成只传 done)。
export interface TodoUpdate {
  title?: string;
  category?: string;
  done?: boolean;
  due_date?: string | null;
}

// 列出全部待办(未完成在前)。失败不谎称"未启用",标记 degraded=true → 前端提示重试。
export async function listTodos(): Promise<TodoList> {
  try {
    const res = await fetch("/api/v1/todos");
    if (!res.ok) return { enabled: false, degraded: true, items: [] };
    const data = (await res.json()) as Partial<TodoList>;
    return {
      enabled: data.enabled ?? false,
      degraded: data.degraded ?? false,
      items: data.items ?? [],
    };
  } catch {
    return { enabled: false, degraded: true, items: [] };
  }
}

// 新建一条待办。待办存储未启用 → ApiError(503)。
export async function createTodo(body: TodoCreate): Promise<Todo> {
  return kfetchJson<Todo>(`/api/v1/todos`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// 局部更新(勾选完成 / 改标题 / 分类 / 截止)。不存在 → ApiError(404)。
export async function updateTodo(id: string, body: TodoUpdate): Promise<Todo> {
  return kfetchJson<Todo>(`/api/v1/todos/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// 删除一条待办(204 无 body)。不存在 → ApiError(404)。
export async function deleteTodo(id: string): Promise<void> {
  return kfetchVoid(`/api/v1/todos/${encodeURIComponent(id)}`, { method: "DELETE" });
}

// ── 调用与费用(管理员;对齐后端 /api/v1/admin/metering)──
//
// 三条口径(与后端 Schema、docs/调用日志与费用统计技术方案.md 一致,界面文案不得偏离):
// - 金额一律是「按配置单价 × 用量」的**估算费用**,不是官方账单(本期不接官方账单);
// - 缺用量或缺价格时 cost_amount 为 null:calls 显示「无法估算」,绝不显示 ¥0;
// - 不同币种、不同用量单位分开返回,不提供跨币种 / 跨单位合计。

// 一次请求按文件 / 页面的估算分摊(批量请求覆盖多文件时有多行)。
export interface MeteringItem {
  document_id: string | null;
  oss_key: string | null;
  page_no: number | null;
  text_count: number; // 该文件在这次请求里的文本条数(分摊权重)
  allocated_cost: string | null; // 分摊到该文件的估算金额;无可分摊金额时为 null
  allocation_note: string;
}

export interface MeteringCall {
  event_id: string;
  occurred_at: string; // UTC ISO;展示时按本地时区
  service: string; // embedding / ocr
  purpose: string; // document_index / query
  provider: string;
  target: string; // 模型名 / OCR Type
  endpoint: string; // 已脱敏(只有主机名)
  call_group: string | null; // 同一逻辑调用的多次尝试共享
  attempt_no: number;
  retry_of: string | null; // 上一次尝试的事件 id
  http_attempts: number; // 本行背后的真实 HTTP 请求次数(SDK 内部重试计入)
  duration_ms: number;
  status: string; // success / failure:业务调用是否成功,不是日志写入状态
  error_class: string;
  error_message: string;
  http_status: number | null;
  provider_request_id: string | null;
  usage_quantity: string | null; // 用量(字符串十进制);null = 未取得
  usage_unit: string; // token / request / page
  usage_source: string; // vendor_response / local_count / unknown
  usage_note: string;
  billing_quantity: string | null; // 计费数量(1k_tokens 会除以 1000)
  billing_unit: string;
  billing_status: string; // billable / unknown(供应商侧是否计费未知)
  billing_note: string;
  cost_amount: string | null; // **估算**费用;null = 无法估算
  currency: string | null; // 币种;不同币种不合并
  cost_status: string; // estimated / unknown
  cost_note: string; // 估算依据 / 无法估算的原因
  price_id: number | null;
  price_version: string | null; // 价目内容摘要(改价后可核对历史用的是哪版)
  price_snapshot: Record<string, unknown> | null; // 事件发生时的不可变快照
  job_id: string | null;
  document_id: string | null;
  oss_key: string | null;
  page_no: number | null;
  items?: MeteringItem[] | null; // 详情接口才有
}

export interface MeteringCallPage {
  total: number;
  offset: number;
  limit: number;
  items: MeteringCall[];
}

export interface MeteringTotals {
  calls: number; // 记录条数
  success: number;
  failure: number;
  unknown_usage: number; // 未取得用量的条数
  unknown_cost: number; // 无法估算费用的条数(缺用量或缺价格)
  billing_unknown: number; // 供应商侧是否计费未知的条数
  http_attempts: number; // 真实发出的 HTTP 请求次数(≥ 记录条数)
}

export interface MeteringServiceStat {
  service: string;
  calls: number;
  success: number;
  failure: number;
  unknown_usage: number;
  unknown_cost: number;
  http_attempts: number;
}

export interface MeteringDayStat {
  day: string; // UTC 日期
  service: string;
  calls: number;
  failure: number;
}

export interface MeteringCostStat {
  service: string;
  currency: string;
  events: number;
  amount: string; // 该服务该币种的估算金额合计(字符串十进制)
}

export interface MeteringUsageStat {
  service: string;
  unit: string;
  quantity: string;
}

export interface MeteringCacheStat {
  layer: string; // ocr_raw / ocr_text / embedding
  unit: string;
  hit: number;
  miss: number;
  shared: number;
  skipped: number;
}

// 日志持久化自身的健康:补写失败 / 队列积压 / 是否有管理员令牌都在这里如实暴露。
export interface MeteringPersistence {
  enabled: boolean;
  configured: boolean;
  running: boolean;
  db_ok: boolean | null; // null = 尚未探测
  db_error: string;
  queued: number; // 队列中待写入
  pending: number; // 补写目录里待写入
  claimed: number; // 正被某个进程写入
  pending_dir: string;
  flushed: number;
  backfilled: number;
  spilled: number;
  lost: number; // >0 表示确有丢失,需人工关注
  last_flush_at: string | null;
  last_error: string;
  price_rules: number;
  price_error: string;
  auth_configured: boolean; // false = admin 接口没配令牌(对外敞开)
  message: string;
}

export interface MeteringSummary {
  since: string | null; // 时间窗起(UTC;含)
  until: string | null; // 时间窗止(UTC;不含)
  filters: Record<string, string | null>;
  error: string; // 非空 = 统计失败,下面的数字不可信(不是「没有调用」)
  totals: MeteringTotals;
  by_service: MeteringServiceStat[];
  by_day: MeteringDayStat[];
  cost_by_service_currency: MeteringCostStat[];
  usage_by_service_unit: MeteringUsageStat[];
  cache: MeteringCacheStat[];
  persistence: MeteringPersistence;
}

export interface MeteringPriceRule {
  id: number | null;
  service: string;
  provider: string;
  target: string;
  unit: string; // 计费单位:1k_tokens / request / page
  currency: string;
  unit_price: string;
  effective_from: string;
  source: string; // 价格来源(官方价格页 / 核实日期)
  note: string;
}

// 列表 / 汇总共用的查询条件(时间传 UTC ISO 串;缺省由后端取最近 7 天)。
export interface MeteringQuery {
  since?: string;
  until?: string;
  service?: string;
  purpose?: string;
  status?: string;
  job_id?: string;
  document_id?: string;
  offset?: number;
  limit?: number;
}

const METERING_PARAMS = [
  "since",
  "until",
  "service",
  "purpose",
  "status",
  "job_id",
  "document_id",
  "offset",
  "limit",
] as const;

// 只把真正传了的条件拼进查询串(空串 = 不筛);offset=0 要留下,不能被当成空值丢掉。
function meteringQuery(q: MeteringQuery): string {
  const p = new URLSearchParams();
  for (const key of METERING_PARAMS) {
    const v = q[key];
    if (v === undefined || v === null || v === "") continue;
    p.set(key, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

// 概览:实际调用 / 成功失败 / 用量(按单位)/ 估算费用(按币种)/ 缓存命中 / 持久化健康。
// 数据库故障时后端仍返回 200,但 summary.error 非空(前端据此提示,而不是当成「没有调用」)。
export async function getMeteringSummary(q: MeteringQuery = {}): Promise<MeteringSummary> {
  return kfetchJson<MeteringSummary>(`/api/v1/admin/metering/summary${meteringQuery(q)}`);
}

// 分页调用日志(时间倒序)。只读:不提供改 / 删 / 清空历史。
export async function listMeteringCalls(q: MeteringQuery = {}): Promise<MeteringCallPage> {
  return kfetchJson<MeteringCallPage>(`/api/v1/admin/metering/calls${meteringQuery(q)}`);
}

// 单条详情:含按文件 / 页面的估算分摊与当时的价目快照。不存在 → ApiError(404)。
export async function getMeteringCall(eventId: string): Promise<MeteringCall> {
  return kfetchJson<MeteringCall>(
    `/api/v1/admin/metering/calls/${encodeURIComponent(eventId)}`,
  );
}

// 当前价目表(估算依据;由运维在 MySQL 的 price_config 里配置)。
export async function listMeteringPrices(): Promise<{ items: MeteringPriceRule[] }> {
  return kfetchJson<{ items: MeteringPriceRule[] }>(`/api/v1/admin/metering/prices`);
}
