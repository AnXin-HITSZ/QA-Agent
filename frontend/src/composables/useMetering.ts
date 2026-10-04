// 调用与费用:分页调用日志 + 概览统计 + 单条详情 + 价目依据。
// 模块级单例:视图与其筛选栏 / 详情抽屉共享同一份状态,不层层透传。
//
// 三条口径(与后端接口、技术方案一致,界面不得偏离):
// - 金额是**估算费用**(按配置单价 × 用量),不是官方账单 —— 本期没接官方账单;
// - 缺用量 / 缺价格时金额为空,显示「无法估算」而不是 ¥0;
// - 调用日志只读(不改 / 不删 / 不清空历史);价目表**只增 + 删** ——
//   改价 = 追加一条生效时间更晚的规则,删除只影响之后的调用(历史按快照估算)。
import { computed, ref } from "vue";

import {
  ApiError,
  createMeteringPrice,
  deleteMeteringPrice,
  getMeteringCall,
  getMeteringSummary,
  listMeteringCalls,
  listMeteringPrices,
  type MeteringCall,
  type MeteringPriceInput,
  type MeteringPriceRule,
  type MeteringQuery,
  type MeteringSummary,
} from "../api";
import { errorText } from "../lib/format";
import { addDaysIso, todayIso } from "../lib/todoDate";

// 时间范围预设。自定义时用两个日期选择器 + 精确到分的时刻输入(合成 datetime-local 串)。
export type RangeKey = "24h" | "7d" | "30d" | "custom";
export type ServiceKey = "" | "embedding" | "ocr";
export type PurposeKey = "" | "document_index" | "query";
export type StatusKey = "" | "success" | "failure";

const RANGE_HOURS: Record<Exclude<RangeKey, "custom">, number> = {
  "24h": 24,
  "7d": 24 * 7,
  "30d": 24 * 30,
};

// 首屏默认最近 7 天(与后端缺省一致):不给前端留「一进来就拉全量历史」的口子。
const PAGE_SIZE = 50;

const range = ref<RangeKey>("7d");
// 自定义范围:本地日历日(YYYY-MM-DD)+ 时刻(HH:mm),仅 range === "custom" 时用。
// 默认给「最近 7 天」,切到自定义就能直接应用,不必先手填四个框。
const customSinceDate = ref<string | null>(addDaysIso(-7));
const customSinceTime = ref("00:00");
const customUntilDate = ref<string | null>(todayIso());
const customUntilTime = ref("23:59");
const service = ref<ServiceKey>("");
const purpose = ref<PurposeKey>("");
const status = ref<StatusKey>("");

const summary = ref<MeteringSummary | null>(null);
const calls = ref<MeteringCall[]>([]);
const total = ref(0);
const offset = ref(0);
const limit = ref(PAGE_SIZE);

const loading = ref(false); // 列表请求中
const summaryLoading = ref(false);
const error = ref(""); // 列表请求失败(网络 / 503)
const summaryError = ref(""); // 概览请求失败;数据库故障走 summary.error(仍是 200)

const prices = ref<MeteringPriceRule[]>([]);
const pricesError = ref("");
const priceSaving = ref(false); // 价目新增 / 删除请求中
const priceError = ref(""); // 价目写入失败(增删共用一处显示)

const detail = ref<MeteringCall | null>(null);
const detailId = ref("");
const detailLoading = ref(false);
const detailError = ref("");

const loaded = ref(false); // 是否成功载入过(KeepAlive 回来时按需刷新)

// ── 时间窗 ──

// 本地 datetime-local 串 → UTC ISO。空串返回 null(交给调用方判断)。
function localToIso(value: string): string | null {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

// 日期 + 时刻 → datetime-local 形状的本地串(YYYY-MM-DDTHH:mm);缺日期 = 未填。
function localDateTime(date: string | null, time: string): string {
  if (!date) return "";
  return `${date}T${/^\d{2}:\d{2}$/.test(time) ? time : "00:00"}`;
}

// 组装查询条件。自定义范围缺项 / 次序颠倒时返回可读错误(不发请求,也不猜时区)。
function buildQuery(withPage: boolean): MeteringQuery | string {
  const q: MeteringQuery = {
    service: service.value,
    purpose: purpose.value,
    status: status.value,
  };
  if (range.value === "custom") {
    const since = localToIso(localDateTime(customSinceDate.value, customSinceTime.value));
    const until = localToIso(localDateTime(customUntilDate.value, customUntilTime.value));
    if (!since || !until) return "自定义时间范围需要同时填「起」「止」。";
    if (new Date(since) >= new Date(until)) return "起始时间必须早于结束时间。";
    q.since = since;
    q.until = until;
  } else {
    const until = new Date();
    q.until = until.toISOString();
    q.since = new Date(until.getTime() - RANGE_HOURS[range.value] * 3600_000).toISOString();
  }
  if (withPage) {
    q.offset = offset.value;
    q.limit = limit.value;
  }
  return q;
}

// ── 载入 ──

let summaryRevision = 0;
let callsRevision = 0;
let detailRevision = 0;

async function loadSummary(q: MeteringQuery): Promise<void> {
  const revision = ++summaryRevision;
  summaryLoading.value = true;
  summaryError.value = "";
  try {
    const result = await getMeteringSummary(q);
    if (revision !== summaryRevision) return;
    summary.value = result;
  } catch (e) {
    if (revision !== summaryRevision) return;
    summary.value = null; // 拿不到就说拿不到,不留旧数字冒充当前窗口
    summaryError.value = errorText(e);
  } finally {
    if (revision === summaryRevision) summaryLoading.value = false;
  }
}

async function loadCalls(q: MeteringQuery): Promise<void> {
  const revision = ++callsRevision;
  loading.value = true;
  error.value = "";
  try {
    const page = await listMeteringCalls(q);
    if (revision !== callsRevision) return;
    calls.value = page.items;
    total.value = page.total;
    loaded.value = true;
  } catch (e) {
    if (revision !== callsRevision) return;
    calls.value = [];
    total.value = 0;
    // 503 = 调用日志数据库不可用(后端已给可读文案);不要显示成「没有调用」。
    error.value =
      e instanceof ApiError && e.status === 503
        ? `调用日志数据库不可用:${e.message}`
        : errorText(e);
  } finally {
    if (revision === callsRevision) loading.value = false;
  }
}

async function loadPrices(): Promise<void> {
  pricesError.value = "";
  try {
    prices.value = (await listMeteringPrices()).items;
  } catch (e) {
    prices.value = [];
    pricesError.value = errorText(e);
  }
}

// 写价目后重新读列表:表格展示以库里的事实为准,不在前端自己拼行。
// 再顺手刷新概览,让「还没有配置价格」提示与 price_rules 计数立刻跟上(不等下次切页)。
// 返回是否成功,交给表单决定收不收面板。
async function addPrice(input: MeteringPriceInput): Promise<boolean> {
  priceSaving.value = true;
  priceError.value = "";
  try {
    await createMeteringPrice(input);
    await loadPrices();
    void refresh();
    return true;
  } catch (e) {
    priceError.value = errorText(e);
    return false;
  } finally {
    priceSaving.value = false;
  }
}

async function removePrice(id: number): Promise<boolean> {
  priceSaving.value = true;
  priceError.value = "";
  try {
    await deleteMeteringPrice(id);
    await loadPrices();
    void refresh();
    return true;
  } catch (e) {
    priceError.value = errorText(e);
    return false;
  } finally {
    priceSaving.value = false;
  }
}

// 拉一次(概览与列表并发;价目表只在首次成功时拉,改价不频繁)。
// 不关详情抽屉:「刷新」只更新数字,不该把正在看的详情顶掉。
async function refresh(): Promise<void> {
  const q = buildQuery(true);
  if (typeof q === "string") {
    error.value = q;
    return;
  }
  // 概览不需要分页参数,其余条件与列表完全一致(两边口径必须能对上)。
  const windowOnly: MeteringQuery = {
    since: q.since,
    until: q.until,
    service: q.service,
    purpose: q.purpose,
    status: q.status,
  };
  await Promise.all([loadSummary(windowOnly), loadCalls(q)]);
  if (!prices.value.length && !pricesError.value) await loadPrices();
}

// 改筛选条件:回到第一页再拉(否则会停在一个已经不存在的页码上),并收起详情。
function applyFilters(): void {
  offset.value = 0;
  closeDetail();
  void refresh();
}

function setRange(key: RangeKey): void {
  range.value = key;
  if (key !== "custom") applyFilters();
}

function nextPage(): void {
  if (offset.value + limit.value >= total.value) return;
  offset.value += limit.value;
  closeDetail();
  void refresh();
}

function prevPage(): void {
  if (offset.value <= 0) return;
  offset.value = Math.max(0, offset.value - limit.value);
  closeDetail();
  void refresh();
}

// ── 详情 ──

async function openDetail(eventId: string): Promise<void> {
  const revision = ++detailRevision;
  detailId.value = eventId;
  detail.value = null;
  detailError.value = "";
  detailLoading.value = true;
  try {
    const result = await getMeteringCall(eventId);
    if (revision !== detailRevision) return;
    detail.value = result;
  } catch (e) {
    if (revision !== detailRevision) return;
    detailError.value = errorText(e);
  } finally {
    if (revision === detailRevision) detailLoading.value = false;
  }
}

function closeDetail(): void {
  ++detailRevision;
  detailId.value = "";
  detail.value = null;
  detailError.value = "";
  detailLoading.value = false;
}

// ── 供视图使用的派生值 ──

const firstIndex = computed(() => (total.value ? offset.value + 1 : 0));
const lastIndex = computed(() => Math.min(offset.value + limit.value, total.value));
const hasPrev = computed(() => offset.value > 0);
const hasNext = computed(() => offset.value + limit.value < total.value);
// 展示用的时间窗:以后端返回的窗口为准(它才知道真实生效的 since / until)。
const windowSince = computed(() => summary.value?.since ?? null);
const windowUntil = computed(() => summary.value?.until ?? null);

export function useMetering() {
  return {
    // 筛选
    range,
    customSinceDate,
    customSinceTime,
    customUntilDate,
    customUntilTime,
    service,
    purpose,
    status,
    // 数据
    summary,
    calls,
    total,
    offset,
    limit,
    prices,
    // 状态
    loading,
    summaryLoading,
    error,
    summaryError,
    pricesError,
    priceSaving,
    priceError,
    loaded,
    // 详情
    detail,
    detailId,
    detailLoading,
    detailError,
    // 派生
    firstIndex,
    lastIndex,
    hasPrev,
    hasNext,
    windowSince,
    windowUntil,
    // 动作
    refresh,
    applyFilters,
    setRange,
    nextPage,
    prevPage,
    openDetail,
    closeDetail,
    loadPrices,
    addPrice,
    removePrice,
  };
}
