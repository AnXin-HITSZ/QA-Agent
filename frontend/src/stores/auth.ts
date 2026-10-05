// 登录态与统一请求封装(认证 / 鉴权 / 用户管理的前端地基)。
//
// 三条铁律(与后端 docs/认证鉴权与用户管理技术方案.md §5 互为表里):
// 1. **access token 只在内存**:模块变量,不进 localStorage / sessionStorage / URL / cookie。
//    刷新页面就没了 —— 靠 HttpOnly 的 refresh cookie 静默换一枚新的(见 bootstrap)。
// 2. **401 只刷新一次**:同一次请求最多重试一次;刷新失败 → 清状态回登录页,不做无限重试。
//    403 是权限问题(不是登录问题),绝不刷新、不重试。
// 3. **并发刷新只发一次**:多处同时撞上 401 时共用同一个 refresh Promise(single-flight)。
//
// 注意:refresh / logout 是**靠 cookie 认证**的接口,所以要带 CSRF 双提交头
// (X-CSRF-Token = qa_csrf cookie 的值);靠 Bearer 认证的接口天然不受 CSRF 影响。
import { reactive } from "vue";

export interface AuthUser {
  id: string;
  email: string;
  display_name: string;
  role: "user" | "admin";
  status: "pending_email" | "pending_approval" | "active" | "rejected" | "disabled";
  email_verified: boolean;
  created_at: string | null;
  last_login_at: string | null;
}

export interface SessionInfo {
  id: string;
  user_agent: string;
  ip: string;
  created_at: string | null;
  last_used_at: string | null;
  expires_at: string | null;
  current: boolean;
}

export interface AdminUser extends AuthUser {
  reviewed_at: string | null;
  reviewed_by: string | null;
  review_note: string;
}

export interface AuditEntry {
  at: string;
  action: string;
  result: string;
  actor_user_id: string | null;
  target_user_id: string | null;
  email: string;
  ip: string;
  note: string;
}

export interface AdminUserPage {
  items: AdminUser[];
  total: number;
  counts: Record<string, number>;
}

// 后端认证错误的统一形状:{"detail": {"code", "message"}}。code 用来分流(前端按它决定
// 提示什么 / 是否重取 CSRF),message 直接给用户看。
export class AuthError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "AuthError";
    this.status = status;
    this.code = code;
  }
}

// 全局登录态。ready=false 时界面还在「首屏静默刷新」阶段(不要急着渲染登录页 —— 那会
// 让已登录用户每次刷新都闪一下登录表单)。
export const authState = reactive<{
  ready: boolean;
  user: AuthUser | null;
  busy: boolean;
  // 一次性提示(改密 / 退出全部之后界面已经回到登录页,没地方显示结果了)。
  // 由 setFlash 写入,登录成功或下一次 clearSession 之前一直挂在登录页上。
  flash: string;
}>({ ready: false, user: null, busy: false, flash: "" });

export function setFlash(message: string): void {
  authState.flash = message;
}

const API = "/api/v1";

// access token:只在内存。expiresAt 是 epoch 毫秒(0 = 没有)。
let accessToken = "";
let expiresAt = 0;
// 并发的 401 共用这一次刷新;null = 当前没有刷新在途。
let refreshing: Promise<boolean> | null = null;
// 登出 / 会话失效时要清掉的前端状态(各 store 的 reset;由 App 注册,避免这里反向依赖组件)。
let signOutHooks: Array<() => void> = [];

export function onSignedOut(hook: () => void): void {
  signOutHooks.push(hook);
}

export function isAdmin(): boolean {
  return authState.user?.role === "admin";
}

// ── cookie / CSRF ──

function readCookie(name: string): string {
  for (const part of document.cookie.split(";")) {
    const [k, ...rest] = part.trim().split("=");
    if (k === name) return decodeURIComponent(rest.join("="));
  }
  return "";
}

// 双提交:头里的值与 qa_csrf cookie 必须一致。cookie 丢了先向 /auth/csrf 取一枚(自愈)。
async function csrfHeaders(force = false): Promise<Record<string, string>> {
  let token = force ? "" : readCookie("qa_csrf");
  if (!token) {
    try {
      const res = await fetch(`${API}/auth/csrf`, { credentials: "same-origin" });
      if (res.ok) {
        const body = (await res.json()) as { csrf_token?: string };
        token = body.csrf_token ?? readCookie("qa_csrf");
      }
    } catch {
      token = ""; // 取不到就照发:让后端按 csrf_missing 明确拒绝,而不是这里猜
    }
  }
  return token ? { "X-CSRF-Token": token } : {};
}

// ── 错误解析 ──

// 优先取后端的中文说明:认证接口是 {"detail":{"code","message"}},其它接口是 {"detail":"…"}。
async function toAuthError(res: Response, fallback: string): Promise<AuthError> {
  let code = "";
  let message = "";
  try {
    const body = (await res.json()) as { detail?: unknown };
    const detail = body?.detail;
    if (typeof detail === "string") {
      message = detail;
    } else if (detail && typeof detail === "object") {
      const d = detail as { code?: unknown; message?: unknown };
      code = typeof d.code === "string" ? d.code : "";
      message = typeof d.message === "string" ? d.message : "";
    }
  } catch {
    // 无 body:用兜底文案
  }
  return new AuthError(res.status, code, message || `${fallback}(HTTP ${res.status})`);
}

// ── 令牌 ──

interface TokenBody {
  access_token: string;
  access_expires_at: string;
  refresh_expires_at: string;
  user: AuthUser;
}

function storeTokens(body: TokenBody): void {
  accessToken = body.access_token;
  const t = Date.parse(body.access_expires_at);
  expiresAt = Number.isFinite(t) ? t : 0;
  authState.user = body.user;
  authState.ready = true;
  authState.flash = ""; // 新登录开始,上一轮留下的结果提示作废
}

function clearTokens(): void {
  accessToken = "";
  expiresAt = 0;
  authState.user = null;
  authState.ready = true;
}

// 清空登录态并广播(登出 / 刷新失败 / 改密后都会走到这里)。清的是**内存状态**:
// refresh cookie 已由后端清掉(或在失效场景本就换不出新令牌)。
export function clearSession(): void {
  clearTokens();
  for (const hook of signOutHooks) {
    try {
      hook();
    } catch {
      // 单个清理钩子出错不影响其它:登出必须总是能走完
    }
  }
}

// ── 刷新(single-flight)──

async function doRefresh(): Promise<boolean> {
  const send = (headers: Record<string, string>): Promise<Response> =>
    fetch(`${API}/auth/refresh`, {
      method: "POST",
      credentials: "same-origin",
      headers,
    });

  let res: Response;
  try {
    res = await send(await csrfHeaders());
  } catch {
    return false; // 网络不通:不当作「会话失效」立即登出(见 callers)
  }
  if (res.status === 403) {
    // CSRF cookie 丢了(浏览器策略 / 用户清过):按后端给的 csrf_missing 自愈一次。
    const err = await toAuthError(res, "请求校验失败");
    if (err.code === "csrf_missing" || err.code === "csrf_mismatch") {
      try {
        res = await send(await csrfHeaders(true));
      } catch {
        return false;
      }
    }
  }
  if (!res.ok) return false;
  storeTokens((await res.json()) as TokenBody);
  return true;
}

// 有且只有一个刷新在途:并发调用共用同一个结果(避免两次刷新把对方轮换掉)。
export function refreshSession(): Promise<boolean> {
  if (!refreshing) {
    refreshing = doRefresh().finally(() => {
      refreshing = null;
    });
  }
  return refreshing;
}

// ── 统一请求封装 ──

// 所有业务请求都从这里走:自动带 Bearer、401 刷新一次再重试、403 原样抛出。
// 重试是安全的:401 由认证依赖在**执行处理函数之前**抛出,请求没有产生任何副作用。
export async function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const withToken = (): RequestInit => ({
    ...init,
    credentials: "same-origin",
    headers: { ...(init.headers ?? {}), ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}) },
  });

  let res = await fetch(input, withToken());
  if (res.status !== 401) return res;
  if (!(await refreshSession())) {
    clearSession(); // 刷新失败 = 会话真的没了:清状态回登录页
    return res;
  }
  res = await fetch(input, withToken());
  if (res.status === 401) clearSession(); // 换了令牌还是 401:账号被停用 / 会话被撤
  return res;
}

// 便捷包装:JSON 响应 + 自动抛 AuthError。K = 走认证错误体,还是普通接口的 {"detail": "…"}。
export async function apiJson<T>(url: string, init: RequestInit = {}, fallback = "请求失败"): Promise<T> {
  let res: Response;
  try {
    res = await apiFetch(url, init);
  } catch {
    throw new AuthError(0, "offline", "暂时连不上服务,请稍后重试。");
  }
  if (!res.ok) throw await toAuthError(res, fallback);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

async function apiVoid(url: string, init: RequestInit = {}, fallback = "操作失败"): Promise<void> {
  let res: Response;
  try {
    res = await apiFetch(url, init);
  } catch {
    throw new AuthError(0, "offline", "暂时连不上服务,请稍后重试。");
  }
  if (!res.ok) throw await toAuthError(res, fallback);
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

// ── 启动:静默恢复登录态 ──

// 首屏调一次。有 refresh cookie 就能换到新的 access token(页面刷新不掉线);
// 没有就停在未登录态。网络不通**不**清 cookie、也**不**报错 —— 真正的失败由后续请求暴露。
export async function bootstrap(): Promise<void> {
  if (authState.ready) return;
  authState.busy = true;
  try {
    await refreshSession();
  } finally {
    authState.busy = false;
    authState.ready = true;
  }
}

// ── 登录 / 注册 / 邮箱验证 ──

export async function login(email: string, password: string): Promise<void> {
  const body = await apiJson<TokenBody>(`${API}/auth/login`, jsonInit("POST", { email, password }), "登录失败");
  storeTokens(body);
}

export async function register(email: string, password: string, displayName: string): Promise<string> {
  const body = await apiJson<{ message: string }>(
    `${API}/auth/register`,
    jsonInit("POST", { email, password, display_name: displayName }),
    "注册失败",
  );
  return body.message;
}

export async function verifyEmail(token: string): Promise<string> {
  const body = await apiJson<{ message: string }>(
    `${API}/auth/verify-email`,
    jsonInit("POST", { token }),
    "验证失败",
  );
  return body.message;
}

export async function resendVerification(email: string): Promise<string> {
  const body = await apiJson<{ message: string }>(
    `${API}/auth/verify-email/resend`,
    jsonInit("POST", { email }),
    "重发失败",
  );
  return body.message;
}

export async function forgotPassword(email: string): Promise<string> {
  const body = await apiJson<{ message: string }>(
    `${API}/auth/password/forgot`,
    jsonInit("POST", { email }),
    "发送失败",
  );
  return body.message;
}

export async function resetPassword(token: string, newPassword: string): Promise<string> {
  const body = await apiJson<{ message: string }>(
    `${API}/auth/password/reset`,
    jsonInit("POST", { token, new_password: newPassword }),
    "重置失败",
  );
  return body.message;
}

// ── 登出 / 我的账号 ──

export async function logout(): Promise<void> {
  try {
    await apiVoid(`${API}/auth/logout`, { method: "POST" }, "退出失败");
  } finally {
    clearSession(); // 服务端失败也照样清本地:用户点了退出就要退出去
  }
}

export async function logoutAll(): Promise<string> {
  const body = await apiJson<{ message: string }>(`${API}/auth/logout-all`, { method: "POST" }, "退出失败");
  clearSession();
  setFlash(body.message); // 「已退出全部设备(共 N 个)」—— 回登录页之后才有地方显示
  return body.message;
}

export async function loadMe(): Promise<AuthUser> {
  const user = await apiJson<AuthUser>(`${API}/auth/me`, {}, "读取账号失败");
  authState.user = user;
  return user;
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<string> {
  const body = await apiJson<{ message: string }>(
    `${API}/auth/password/change`,
    jsonInit("POST", { current_password: currentPassword, new_password: newPassword }),
    "修改失败",
  );
  clearSession(); // 后端已撤销全部会话(含本次),前端同步回登录页
  return body.message;
}

export async function listSessions(): Promise<SessionInfo[]> {
  const body = await apiJson<{ items: SessionInfo[] }>(`${API}/auth/sessions`, {}, "读取设备失败");
  return body.items;
}

export async function revokeSession(sessionId: string): Promise<void> {
  await apiVoid(`${API}/auth/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" }, "下线失败");
}

// ── 管理员:用户管理 / 审计 ──

export async function listUsers(params: {
  status?: string;
  q?: string;
  offset?: number;
  limit?: number;
} = {}): Promise<AdminUserPage> {
  const p = new URLSearchParams();
  if (params.status) p.set("status", params.status);
  if (params.q) p.set("q", params.q);
  if (params.offset) p.set("offset", String(params.offset));
  if (params.limit) p.set("limit", String(params.limit));
  const qs = p.toString();
  return apiJson<AdminUserPage>(`${API}/admin/users${qs ? `?${qs}` : ""}`, {}, "读取用户失败");
}

export type AdminAction = "approve" | "reject" | "disable" | "enable";

export async function reviewUser(userId: string, action: AdminAction, note = ""): Promise<AdminUser> {
  return apiJson<AdminUser>(
    `${API}/admin/users/${encodeURIComponent(userId)}/${action}`,
    jsonInit("POST", { note }),
    "操作失败",
  );
}

export async function setUserRole(userId: string, role: "user" | "admin"): Promise<AdminUser> {
  return apiJson<AdminUser>(
    `${API}/admin/users/${encodeURIComponent(userId)}/role`,
    jsonInit("POST", { role }),
    "改角色失败",
  );
}

export async function listAudit(targetUserId?: string, limit = 100): Promise<AuditEntry[]> {
  const p = new URLSearchParams({ limit: String(limit) });
  if (targetUserId) p.set("target_user_id", targetUserId);
  const body = await apiJson<{ items: AuditEntry[] }>(`${API}/admin/audit?${p}`, {}, "读取审计失败");
  return body.items;
}
