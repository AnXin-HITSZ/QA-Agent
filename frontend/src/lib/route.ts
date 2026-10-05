// 视图路由:沿用项目原有的 hash 路由(#/xxx),不引 vue-router。
//
// 为什么把邮件链接先「搬进 hash」:后端发出的链接是 `{前端地址}/verify-email?token=…`
// (路径 + 查询串)。锚点(# 之后)不会发给服务器 —— 搬进 hash 之后,这个一次性令牌就不会
// 留在 nginx / 代理的访问日志里。搬完统一由 parseRoute 解析,全站只有一种路由写法。
export type RouteName =
  | "login"
  | "register"
  | "verify"
  | "reset"
  | "forgot"
  | "account"
  | "users"
  | "chat"
  | "knowledge"
  | "sops"
  | "metering";

// 登录后的应用内视图(页头标签 + 我的账号 + 用户管理)。
// 与 RouteName 分开取名:AppHeader 的标签高亮只认这几个,认证页传不进去。
export type AppRouteName = "chat" | "knowledge" | "sops" | "metering" | "account" | "users";

export interface Route {
  name: RouteName;
  token: string; // verify / reset 路由从邮件链接里带过来的一次性令牌
}

const NAMES: readonly RouteName[] = [
  "login", "register", "verify", "reset", "forgot",
  "account", "users", "chat", "knowledge", "sops", "metering",
];

// 未登录时可以停在这几个视图上;其余(应用内视图)一律回登录页。
const AUTH_ROUTES: readonly RouteName[] = ["login", "register", "verify", "reset", "forgot"];

export function isAuthRoute(name: RouteName): boolean {
  return AUTH_ROUTES.includes(name);
}

export function isAppRoute(name: RouteName): name is AppRouteName {
  return !isAuthRoute(name);
}

function asName(value: string): RouteName | null {
  return (NAMES as readonly string[]).includes(value) ? (value as RouteName) : null;
}

export function parseRoute(hash = location.hash): Route {
  const raw = hash.replace(/^#\/?/, "");
  const at = raw.indexOf("?");
  const path = at >= 0 ? raw.slice(0, at) : raw;
  const query = at >= 0 ? raw.slice(at + 1) : "";
  const name = asName(path) ?? "chat";
  return { name, token: new URLSearchParams(query).get("token") ?? "" };
}

export function href(name: RouteName, params: Record<string, string> = {}): string {
  const q = new URLSearchParams(params).toString();
  return `#/${name}${q ? `?${q}` : ""}`;
}

export function navigate(name: RouteName, params: Record<string, string> = {}): void {
  const next = href(name, params);
  if (location.hash === next) {
    // 同址(如验证失败后原地重试):手动广播一次,让监听者重新解析。
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    return;
  }
  location.hash = next;
}

// 首屏把「路径形式的邮件链接」规范化成 hash 形式(见文件头说明)。只改一次,不新增历史条目。
export function normalizeEntry(): void {
  const path = location.pathname.replace(/\/+$/, "");
  const m = /^\/(verify-email|reset-password)$/.exec(path);
  if (!m) return;
  const name: RouteName = m[1] === "verify-email" ? "verify" : "reset";
  history.replaceState(null, "", `/#/${name}${location.search}`);
}
