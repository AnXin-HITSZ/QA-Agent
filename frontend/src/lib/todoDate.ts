// 待办日期的解析与展示口径:抽屉列表与日期选择器共用一份,免得两处文案 / 算法漂移。
//
// 约定:待办日期一律是**本地日历日**,字符串形如 YYYY-MM-DD。解析时补 T00:00:00 走本地
// 时区,格式化时按 年/月/日 逐段拼 —— **绝不 toISOString()**(它按 UTC 换算,东八区下
// 会把 09-25 串成 09-24)。后端只收发这个字符串,不做时区解释。

// 补零到两位。
export function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

// 本地 Date → YYYY-MM-DD。
export function toIsoDate(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

// 今天(本地日历日)。
export function todayIso(): string {
  return toIsoDate(new Date());
}

// 今天起偏移 days 天(负数为过去)。
export function addDaysIso(days: number): string {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() + days);
  return toIsoDate(d);
}

// 解析 YYYY-MM-DD(本地零点);空值 / 非法一律 null。
export function parseIsoDate(s: string | null): Date | null {
  if (!s) return null;
  const d = new Date(`${s}T00:00:00`);
  return Number.isNaN(d.getTime()) ? null : d;
}

// 与「今天」相差的天数:昨天 = -1,今天 = 0,明天 = +1。非法 / 空 → null。
export function daysFromToday(s: string | null): number | null {
  const d = parseIsoDate(s);
  if (!d) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  d.setHours(0, 0, 0, 0);
  return Math.round((d.getTime() - today.getTime()) / 86400000);
}

// 相对期限的核心文案:「09-25 · 还剩 3 天」/「09-22 · 今天到期」/「09-20 · 已逾期 2 天」。
function dueCore(d: Date, diff: number): string {
  const md = `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  if (diff < 0) return `${md} · 已逾期 ${-diff} 天`;
  if (diff === 0) return `${md} · 今天到期`;
  return `${md} · 还剩 ${diff} 天`;
}

// 列表里用:条目没有标签兜底,补「截止」二字 →「截止 09-25 · 还剩 3 天」。无值 / 非法 → 空串。
export function formatDue(s: string | null): string {
  const d = parseIsoDate(s);
  const diff = daysFromToday(s);
  if (!d || diff === null) return "";
  return `截止 ${dueCore(d, diff)}`;
}

// 表单里用:标签已写明「截止日期」,不再重复前缀 →「09-25 · 还剩 3 天」。
export function formatDueShort(s: string | null): string {
  const d = parseIsoDate(s);
  const diff = daysFromToday(s);
  if (!d || diff === null) return "";
  return dueCore(d, diff);
}

// 是否已过截止日(今天到期不算逾期)。「是否已完成」由调用方另判。
export function isOverdue(s: string | null): boolean {
  const diff = daysFromToday(s);
  return diff !== null && diff < 0;
}
