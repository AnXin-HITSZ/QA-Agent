// 展示口径:跨视图共用的少量格式化 —— 同一份文案只写一处,免得各视图的兜底慢慢漂开。

// 后端给的时间戳是 Unix 秒(不是 ISO 字符串);空值 / 非法一律空串,不显示「Invalid Date」。
export function formatWhen(sec: number | null): string {
  if (!sec) return "";
  const d = new Date(sec * 1000);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("zh-CN", { year: "numeric", month: "numeric", day: "numeric" });
}

// 异常 → 可展示的一行文案:Error / ApiError 取其 message,其余(字符串、对象)转串。
export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// 后端的 UTC ISO 时间戳 → 本地时区的「MM-DD HH:mm:ss」。调用日志按时间排障,精确到秒不够看
// 毫秒的场合同一时刻会有多条;这里给到秒即可,空的 / 非法的一律空串(不显示 Invalid Date)。
export function formatStamp(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number): string => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// 时间戳 → 本地时区的完整时间(用于详情 / 统计窗口说明:说清是哪一段时间,别让人自己换算)。
export function formatStampFull(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number): string => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// 用量 / 金额的十进制字符串 → 去掉多余的尾零再展示(后端给的是精确十进制串,不是浮点数)。
// 至少保留 minFrac 位,避免把 0.00 显示成 0;超长小数保留 maxFrac 位后截断(金额本身仍是精确值)。
export function trimDecimal(value: string | null | undefined, minFrac = 2, maxFrac = 8): string {
  if (value === null || value === undefined || value === "") return "";
  const s = String(value);
  if (!/^-?\d+(\.\d+)?$/.test(s)) return s;
  const [int, frac = ""] = s.split(".");
  const cut = frac.slice(0, maxFrac).replace(/0+$/, "");
  const kept = cut.padEnd(minFrac, "0");
  return kept ? `${int}.${kept}` : int;
}

// 毫秒耗时 → 人读的短文案(秒级以上换单位,免得盯着 4 位数字自己数)。
export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}
