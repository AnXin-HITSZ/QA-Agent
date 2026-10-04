// 调用与费用:分页调用日志 + 概览统计 + 单条详情 + 价目依据。
// 模块级单例:视图与其筛选栏 / 详情抽屉共享同一份状态,不层层透传。
//
// 三条口径(与后端接口、技术方案一致,界面不得偏离):
// - 金额是**估算费用**(按配置单价 × 用量),不是官方账单 —— 本期没接官方账单;
// - 缺用量 / 缺价格时金额为空,显示「无法估算」而不是 ¥0;
// - 只看不写:接口不提供改 / 删 / 清空历史。
import { computed, ref } from "vue";

import {
  ApiError,
  getMeteringCall,
  getMeteringSummary,
  listMeteringCalls,
  listMeteringPrices,
  type MeteringCall,
  type MeteringPriceRule,
  type MeteringQuery,
  type MeteringSummary,
} from "../api";
import { errorText } from "../lib/format";

// 时间范围预设。自定义时用两个本地时间输入框(datetime-local)。
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
const customSince = ref(""); // datetime-local 的值(本地时区),仅 range === "custom" 时用
const customUntil = ref("");
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

// 组装查询条件。自定义范围缺项 / 次序颠倒时返回可读错误(不发请求,也不猜时区)。
function buildQuery(withPage: boolean): MeteringQuery | string {
  const q: MeteringQuery = {
    service: service.value,
    purpose: purpose.value,
    status: status.value,
  };
  if (range.value === "custom") {
    const since = localToIso(customSince.value);
    const until = localToIso(customUntil.value);
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

async function loadSummary(q: MeteringQuery): Promise<void> {
  summaryLoading.value = true;
  summaryError.value = "";
  try {
    summary.value = await getMeteringSummary(q);
  } catch (e) {
    summary.value = null; // 拿不到就说拿不到,不留旧数字冒充当前窗口
    summaryError.value = errorText(e);
  } finally {
    summaryLoading.value = false;
  }
}

async function loadCalls(q: MeteringQuery): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const page = await listMeteringCalls(q);
    calls.value = page.items;
    total.value = page.total;
    loaded.value = true;
  } catch (e) {
    calls.value = [];
    total.value = 0;
    // 503 = 调用日志数据库不可用(后端已给可读文案);不要显示成「没有调用」。
    error.value =
      e instanceof ApiError && e.status === 503
        ? `调用日志数据库不可用:${e.message}`
        : errorText(e);
  } finally {
    loading.value = false;
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
  detailId.value = eventId;
  detail.value = null;
  detailError.value = "";
  detailLoading.value = true;
  try {
    detail.value = await getMeteringCall(eventId);
  } catch (e) {
    detailError.value = errorText(e);
  } finally {
    detailLoading.value = false;
  }
}

function closeDetail(): void {
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
    customSince,
    customUntil,
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
  };
}
