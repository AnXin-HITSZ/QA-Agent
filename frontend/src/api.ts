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

export interface ChatResponse {
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
  onDone?: (skill: string | null, sources: Source[]) => void;
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
    throw new Error("没连上后端。确认后端已在 127.0.0.1:8000 运行,然后重试。");
  }
  if (!res.ok) {
    throw new Error(`后端返回错误(HTTP ${res.status})。稍后重试,或查看后端日志。`);
  }
  return (await res.json()) as ChatResponse;
}

// EventSource 不支持 POST,这里用 fetch + ReadableStream 手动解析 SSE 帧(无新依赖)。
export async function chatStream(
  message: string,
  handlers: StreamHandlers,
  threadId?: string | null,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/v1/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, thread_id: threadId ?? null } satisfies ChatRequest),
    });
  } catch {
    throw new Error("没连上后端。确认后端已在 127.0.0.1:8000 运行,然后重试。");
  }
  if (!res.ok || !res.body) {
    throw new Error(`后端返回错误(HTTP ${res.status})。稍后重试,或查看后端日志。`);
  }

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
        handlers.onDone?.(
          (payload?.skill as string) ?? null,
          (payload?.sources as Source[]) ?? [],
        );
        break;
    }
  };

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const sep = /\r?\n\r?\n/;
  let buf = "";
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
  }
  if (buf.trim()) dispatch(buf); // flush 尾帧
}

// ── 历史对话:后端以 Redis 为准,扫描 / 读取 / 删除会话 ──

export interface ConversationSummary {
  thread_id: string;
  title: string;
  message_count: number;
  updated_at: string | null; // 最新 checkpoint 的 ISO 时间;null = 无
}

export interface ConversationList {
  enabled: boolean; // 后端 Redis 跨轮记忆是否配置启用;false = 未配置(单轮模式)
  degraded: boolean; // 已启用但本次读取失败(超时/Redis 错误);true 时列表恒空、提示重试
  items: ConversationSummary[];
}

export interface ConversationMessage {
  role: "user" | "assistant";
  content: string;
}

export interface ConversationDetail {
  thread_id: string;
  messages: ConversationMessage[];
}

// 列出全部历史会话(最近活跃在前)。
// 请求失败(后端不可用 / 非 2xx)不谎称"未启用",而是标记 degraded=true → 前端提示可重试。
export async function listConversations(): Promise<ConversationList> {
  try {
    const res = await fetch("/api/v1/conversations");
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

// 单文件增量索引结果。indexed=false 时 reason 说明未索引原因(needs_ocr / unsupported / error…)。
export interface IndexFileResult {
  key: string;
  indexed: boolean;
  chunks: number; // 写入的切块 / 向量数
  reason: string | null;
}

export interface IndexedKeysResult {
  prefix: string;
  keys: string[]; // 该节点下已建立索引的原件 key
}

// 全量重建结果(维护兜底用)。
export interface ReindexResult {
  collection: string;
  prefix: string;
  total_files: number;
  indexed_files: number;
  skipped_files: number;
  chunks: number;
  vectors: number;
  skipped: { key: string; reason: string }[];
  skipped_truncated: boolean;
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
    throw new ApiError(0, "没连上后端。确认后端已在 127.0.0.1:8000 运行,然后重试。");
  }
  if (!res.ok) throw new ApiError(res.status, await detailOr(res, "请求失败"));
  return (await res.json()) as T;
}

async function kfetchVoid(url: string, init?: RequestInit): Promise<void> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    throw new ApiError(0, "没连上后端。确认后端已在 127.0.0.1:8000 运行,然后重试。");
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

// 增量索引单个原件(上传成功后逐个调用)。Embeddings/Qdrant 未配 → ApiError(503)。
export async function indexFile(key: string): Promise<IndexFileResult> {
  return kfetchJson<IndexFileResult>(`/api/v1/admin/knowledge/index`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key }),
  });
}

// 列出某节点下已建立索引的原件 key(供浏览时显示已/未索引徽标)。Qdrant 未配 → ApiError(503)。
export async function indexedKeys(prefix = ""): Promise<IndexedKeysResult> {
  return kfetchJson<IndexedKeysResult>(
    `/api/v1/admin/knowledge/indexed?prefix=${encodeURIComponent(prefix)}`,
  );
}

// 全量重建索引(维护兜底):留空 prefix = 整库,否则只重建该子树。
export async function reindexKnowledge(prefix = ""): Promise<ReindexResult> {
  return kfetchJson<ReindexResult>(`/api/v1/admin/knowledge/reindex`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prefix }),
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

// 列出全部 SOP(不含正文,按 id 升序)。OSS 未配置 → 抛 ApiError(status 503)。
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
