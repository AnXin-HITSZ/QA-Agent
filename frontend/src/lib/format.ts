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
