<script setup lang="ts">
// 调用与费用:实际外部调用(embedding / OCR)的日志与**估算**费用统计。
//
// 界面口径(与后端接口、docs/调用日志与费用统计技术方案.md 一致,改文案前先改文档):
// - 金额一律标「估算」:按配置单价 × 用量算出来的,不是官方账单,也不扣免费额度 / 折扣;
// - 拿不到用量或缺价格时显示「无法估算」+ 原因,绝不显示 ¥0;
// - 缓存命中与实际调用分列:命中不产生外部调用,也就不产生费用;
// - 业务失败(供应商返回失败 / 超时)与日志补写失败(写库不通)是两回事,分开提示。
import { computed, onActivated, onErrorCaptured, reactive, ref } from "vue";

import "../styles/metering.css";
import {
  useMetering,
  type PurposeKey,
  type RangeKey,
  type ServiceKey,
  type StatusKey,
} from "../composables/useMetering";
import { formatMs, formatStamp, formatStampFull, trimDecimal } from "../lib/format";
import { todayIso } from "../lib/todoDate";
import DateField from "./DateField.vue";
import DateTimeField from "./DateTimeField.vue";
import MeteringDetail from "./MeteringDetail.vue";
import SelectField from "./SelectField.vue";

const {
  range,
  customSinceDate,
  customSinceTime,
  customUntilDate,
  customUntilTime,
  service,
  purpose,
  status,
  summary,
  calls,
  total,
  loading,
  summaryLoading,
  error,
  summaryError,
  prices,
  pricesError,
  priceSaving,
  priceError,
  detailId,
  firstIndex,
  lastIndex,
  hasPrev,
  hasNext,
  windowSince,
  windowUntil,
  applyFilters,
  setRange,
  nextPage,
  prevPage,
  openDetail,
  refresh,
  loadPrices,
  addPrice,
  removePrice,
} = useMetering();

// KeepAlive 保活:每次切回本视图都拉一次最新数字(逐条看费用的场景,旧数字比多一次请求更糟)。
onActivated(() => {
  void refresh();
});

// ── 错误边界:渲染期异常不白屏,给成因 + 一键重试(与 SOP 视图同一手法) ──
const renderFailed = ref(false);
const failMessage = ref("");
onErrorCaptured((err) => {
  renderFailed.value = true;
  failMessage.value = err instanceof Error ? err.message : String(err);
});

// 标签写整条:胶囊与触发器共用同一份文案(「今天」放进后缀模板会拼成「最近 今天」)。
const RANGES: { key: RangeKey; label: string }[] = [
  { key: "24h", label: "最近 24 小时" },
  { key: "today", label: "今天" },
  { key: "7d", label: "最近 7 天" },
  { key: "30d", label: "最近 30 天" },
  { key: "custom", label: "自定义" },
];
// 三个筛选下拉的选项:首项值都是空串 = 不过滤(后端口径:不传即不限)。
// 列表是自绘的,不再有原生 title 提示,所以文案直接写全(如「索引(写库)」)。
const serviceOptions: { value: ServiceKey; label: string }[] = [
  { value: "", label: "全部服务" },
  { value: "embedding", label: "embedding(向量化)" },
  { value: "ocr", label: "ocr(文字 / 票据识别)" },
];
const purposeOptions: { value: PurposeKey; label: string }[] = [
  { value: "", label: "全部用途" },
  { value: "document_index", label: "索引(写库)" },
  { value: "query", label: "检索(问答)" },
];
const statusOptions: { value: StatusKey; label: string }[] = [
  { value: "", label: "全部状态" },
  { value: "success", label: "成功" },
  { value: "failure", label: "失败(业务调用失败,与日志补写失败无关)" },
];

const activeTab = ref("logs");
const rangeDialog = ref<HTMLDialogElement | null>(null);
// 自定义范围的草稿:日期与时刻分开存(与组合框的两个 v-model 一一对应),
// 应用时再合成 datetime-local 串;日期可以清空(null)回到占位,由校验拦下。
const draft = reactive<{
  sinceDate: string | null; sinceTime: string; untilDate: string | null; untilTime: string;
}>({ sinceDate: null, sinceTime: "00:00", untilDate: null, untilTime: "23:59" });
const rangeError = ref("");
// 对话框每次关窗 +1,给两个组合框当 key 强制重挂载:浮层(日历 / 时刻)的展开状态
// 跟着对话框走,不然取消后重开会看到上次没关的浮层还挂在那里。
const rangeEpoch = ref(0);
const rangeLabel = computed(() => range.value === "custom" ? `${customSinceDate.value} — ${customUntilDate.value}` : RANGES.find(r => r.key === range.value)?.label ?? "自定义");
function openRange(): void {
  draft.sinceDate = customSinceDate.value;
  draft.sinceTime = customSinceTime.value;
  draft.untilDate = customUntilDate.value;
  draft.untilTime = customUntilTime.value;
  rangeError.value = "";
  rangeDialog.value?.showModal();
}
function onRangeBackdrop(event: MouseEvent): void { if (event.target === rangeDialog.value) closeRange(); }
function closeRange(): void { rangeDialog.value?.close(); }
function chooseRange(key: RangeKey): void { setRange(key); closeRange(); }
function applyRange(): void {
  const since = draft.sinceDate ? `${draft.sinceDate}T${draft.sinceTime}` : "";
  const until = draft.untilDate ? `${draft.untilDate}T${draft.untilTime}` : "";
  if (!since || !until || !Number.isFinite(Date.parse(since)) || !Number.isFinite(Date.parse(until)) || new Date(since) >= new Date(until)) {
    rangeError.value = "请选择有效时间，结束时间须晚于开始时间。"; return;
  }
  customSinceDate.value = draft.sinceDate; customSinceTime.value = draft.sinceTime;
  customUntilDate.value = draft.untilDate; customUntilTime.value = draft.untilTime;
  range.value = "custom"; applyFilters(); closeRange();
}
function resetFilters(): void { service.value = ""; purpose.value = ""; status.value = ""; setRange("7d"); }
const summaryReady = computed(() => Boolean(summary.value && !summary.value.error && !summaryError.value && summary.value.persistence?.enabled && summary.value.persistence?.db_ok !== false));

const totals = computed(() => summary.value?.totals ?? null);
const persistence = computed(() => summary.value?.persistence ?? null);
const serviceRows = computed(() => summary.value?.by_service ?? []);
const usageRows = computed(() => summary.value?.usage_by_service_unit ?? []);
const costRows = computed(() => summary.value?.cost_by_service_currency ?? []);
const cacheRows = computed(() => summary.value?.cache ?? []);
// 只有缓存记录的那个服务 / 时间窗里才谈得上「命中」 —— 命中数直接由后端分列给出。
const cacheHits = computed(() => cacheRows.value.reduce((n, c) => n + c.hit, 0));
const cacheMiss = computed(() => cacheRows.value.reduce((n, c) => n + c.miss, 0));
const cacheShared = computed(() => cacheRows.value.reduce((n, c) => n + c.shared, 0));
const pricesMissing = computed(() => persistence.value?.price_rules === 0);
// 只画最近 14 天的相对量(by_day 是 UTC 日期,跨时区的「今天」以 UTC 为准,避免两端对不上)。
const DAYS_SHOWN = 14;
const dayRows = computed(() => {
  const merged = new Map<string, { day: string; calls: number; failure: number }>();
  for (const d of summary.value?.by_day ?? []) {
    const row = merged.get(d.day) ?? { day: d.day, calls: 0, failure: 0 };
    row.calls += d.calls;
    row.failure += d.failure;
    merged.set(d.day, row);
  }
  return [...merged.values()].sort((a, b) => (a.day < b.day ? 1 : -1)).slice(0, DAYS_SHOWN);
});
const dayMax = computed(() => Math.max(1, ...dayRows.value.map((d) => d.calls)));

// 缓存三层的含义必须写清楚:OCR 原始命中不产生外部调用,文本转换是本地的,不省钱可谈。
const LAYER_TEXT: Record<string, string> = {
  ocr_raw: "OCR 原始结果(按页):命中即不再发起识别请求",
  ocr_text: "OCR 文本转换(本地):命中不重跑转换,也不涉及外部调用",
  embedding: "embedding 向量(按文本条数):只有未命中才会真的去调供应商",
};
const SERVICE_TEXT: Record<string, string> = { embedding: "向量化", ocr: "OCR 识别" };
const PURPOSE_TEXT: Record<string, string> = { document_index: "索引", query: "检索" };

const filtersActive = computed(
  () => Boolean(service.value || purpose.value || status.value || range.value === "custom"),
);

function basename(key: string | null): string {
  if (!key) return "";
  const cut = key.lastIndexOf("/");
  return cut >= 0 ? key.slice(cut + 1) : key;
}

function onRowKey(e: KeyboardEvent, eventId: string): void {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    void openDetail(eventId);
  }
}

// ── 价目表维护(只增 + 删:改价 = 追加一条生效时间更晚的规则,不覆盖历史) ──

const SERVICE_OPTS: { key: string; label: string }[] = [
  { key: "embedding", label: "embedding(向量化)" },
  { key: "ocr", label: "ocr(文字 / 票据)" },
];
const UNIT_OPTS: { key: string; label: string; title: string }[] = [
  { key: "1k_tokens", label: "1k_tokens", title: "每千 token" },
  { key: "request", label: "request", title: "每次请求" },
  { key: "page", label: "page", title: "每页(如 OCR 按页计费)" },
];

const priceFormOpen = ref(false);
const priceFormError = ref("");
const confirmDel = ref<number | null>(null);
// 生效时间默认从今天 00:00 起:今天核实的价格今天就开始算,时刻可再调。
const priceDate = ref<string | null>(todayIso());
const priceTime = ref("00:00");
const priceForm = reactive({
  service: "embedding",
  provider: "",
  target: "",
  unit: "1k_tokens",
  currency: "CNY",
  unit_price: "",
  source: "",
  note: "",
});

function resetPriceForm(): void {
  Object.assign(priceForm, {
    service: "embedding",
    provider: "",
    target: "",
    unit: "1k_tokens",
    currency: "CNY",
    unit_price: "",
    source: "",
    note: "",
  });
  priceDate.value = todayIso();
  priceTime.value = "00:00";
  priceFormError.value = "";
}

function openPriceForm(): void {
  priceFormOpen.value = true;
  priceError.value = ""; // 别把上一次的删 / 增失败带进新表单
  priceFormError.value = "";
}

function closePriceForm(): void {
  priceFormOpen.value = false;
  resetPriceForm();
}

// 前端只挡明显填错(空值、格式),与后端校验同样的口径但不重复实现业务规则。
function validatePriceForm(): string {
  if (!priceForm.provider.trim()) return "供应商不能为空(如 dashscope)。";
  if (!/^\d+(\.\d{1,8})?$/.test(priceForm.unit_price.trim())) {
    return "单价填数字,最多 8 位小数,不要带货币符号或千分位。";
  }
  if (!/^[A-Za-z]{3}$/.test(priceForm.currency.trim())) return "币种填 3 位字母代码(如 CNY)。";
  if (!priceDate.value) return "请选择生效日期。";
  if (!/^\d{2}:\d{2}$/.test(priceTime.value)) return "生效时刻按 HH:mm 填写。";
  if (!priceForm.source.trim()) {
    return "来源不能为空:写清官方价格页并注明核实日期,以后复核靠它。";
  }
  return "";
}

async function submitPrice(): Promise<void> {
  priceFormError.value = validatePriceForm();
  if (priceFormError.value) return;
  // 本地日期 + 时刻 → UTC ISO,时区偏移由浏览器给出,后端不需要猜。
  const eff = new Date(`${priceDate.value}T${priceTime.value}:00`);
  if (Number.isNaN(eff.getTime())) {
    priceFormError.value = "生效时间不合法,请重新选择日期与时刻。";
    return;
  }
  const ok = await addPrice({
    service: priceForm.service,
    provider: priceForm.provider.trim(),
    target: priceForm.target.trim(),
    unit: priceForm.unit,
    currency: priceForm.currency.trim().toUpperCase(),
    unit_price: priceForm.unit_price.trim(),
    effective_from: eff.toISOString(),
    source: priceForm.source.trim(),
    note: priceForm.note.trim(),
  });
  if (ok) closePriceForm();
}

async function onRemovePrice(id: number | null): Promise<void> {
  if (id === null) return;
  if (await removePrice(id)) confirmDel.value = null;
}
</script>

<template>
  <!-- 渲染出错兜底:整块不再是空白,给出成因 + 一键重试 -->
  <main v-if="renderFailed" class="mt">
    <div class="mt__wrap">
      <section class="mt__state">
        <p class="mt__stateHd">页面渲染出错</p>
        <p class="mt__stateBody">{{ failMessage || "发生未知错误。" }}</p>
        <button
          class="mt__retry"
          type="button"
          @click="
            renderFailed = false;
            failMessage = '';
            refresh();
          "
        >
          重试
        </button>
      </section>
    </div>
  </main>

  <main v-else class="mt">
    <div class="mt__wrap">
      <header class="mt__bar">
        <div>
          <h2 class="mt__title">调用与费用</h2>
          <p class="mt__sub">追踪 Embedding 与 OCR 调用，了解用量与估算费用。</p>
        </div>
        <button class="mt__btn" type="button" :disabled="loading" @click="refresh">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
      </header>

      <div class="mt__filters">
        <button class="mt__btn mt__dateTrigger" type="button" aria-haspopup="dialog" @click="openRange">
          <span class="mt__dateTriggerT">
            <svg class="mt__dtIc" viewBox="0 0 16 16" aria-hidden="true">
              <circle cx="8" cy="8" r="5.6" />
              <polyline points="8,4.8 8,8 10.4,9.6" />
            </svg>
            {{ rangeLabel }}
          </span>
          <svg class="mt__dtCv" viewBox="0 0 12 12" aria-hidden="true">
            <polyline points="2.5,4.5 6,8 9.5,4.5" />
          </svg>
        </button>
        <SelectField v-model="service" label="服务" :options="serviceOptions" @change="applyFilters" />
        <SelectField v-model="purpose" label="用途" :options="purposeOptions" @change="applyFilters" />
        <SelectField v-model="status" label="状态" :options="statusOptions" @change="applyFilters" />
        <button class="mt__textBtn" :disabled="!filtersActive && range === '7d'" @click="resetFilters">重置</button>
      </div>
      <dialog ref="rangeDialog" class="mt__dateDialog" aria-labelledby="range-title" @click="onRangeBackdrop" @close="rangeEpoch++">
        <div class="mt__dateContent">
          <div class="mt__dialogHead"><h3 id="range-title">选择时间范围</h3><button class="mt__textBtn" aria-label="关闭时间选择" @click="closeRange">✕</button></div>
          <div class="flt__chips"><button v-for="r in RANGES.filter(r => r.key !== 'custom')" :key="r.key" class="flt__chip" :class="{ 'is-on': range === r.key }" @click="chooseRange(r.key)">{{ r.label }}</button></div>
          <p class="mt__sub">自定义范围 · 本地时间</p>
          <div class="mt__field">
            <div class="mt__fieldLabel">
              <span class="mt__fieldName">开始时间</span>
              <button v-if="draft.sinceDate" class="mt__fieldClear" type="button" @click="draft.sinceDate = null">清除</button>
            </div>
            <DateTimeField :key="rangeEpoch" v-model:date="draft.sinceDate" v-model:time="draft.sinceTime" />
          </div>
          <div class="mt__field">
            <div class="mt__fieldLabel">
              <span class="mt__fieldName">结束时间</span>
              <button v-if="draft.untilDate" class="mt__fieldClear" type="button" @click="draft.untilDate = null">清除</button>
            </div>
            <DateTimeField :key="rangeEpoch" v-model:date="draft.untilDate" v-model:time="draft.untilTime" eod />
          </div>
          <p class="mt__sub">统计至结束时刻之前；修改后点击应用生效。</p>
          <p v-if="rangeError" class="mt__err" role="alert">{{ rangeError }}</p>
          <div class="mt__dialogActions"><button class="mt__cancel" @click="closeRange">取消</button><button class="mt__save" @click="applyRange">应用范围</button></div>
        </div>
      </dialog>

      <!-- 口径与健康提示(按重要性依次出现) -->
      <p v-if="persistence && !persistence.enabled" class="mt__note">
        <strong>调用日志与费用统计未启用。</strong
        >{{ persistence.message || "请在 backend/.env 配置 METERING_MYSQL_URL 后重启后端。" }}
        未配置不影响 OCR / 索引 / 检索,只是不留调用记录。
      </p>
      <p v-if="persistence && persistence.enabled && persistence.db_ok === false" class="mt__note mt__note--warn">
        <strong>调用日志数据库当前连不上:</strong>{{ persistence.db_error || "连接失败" }}。
        新的调用会转入补写目录,数据库恢复后自动补写(只补日志,不会重做已经完成的付费调用)。
      </p>
      <p v-if="summary && summary.error" class="mt__note mt__note--warn">
        <strong>统计失败:</strong>{{ summary.error }}下面的数字不可信,不等于「没有调用」。
      </p>
      <p v-if="summaryError" class="mt__note mt__note--warn">
        <strong>概览请求失败:</strong>{{ summaryError }}
      </p>
      <p v-if="persistence && persistence.lost > 0" class="mt__note mt__note--warn">
        <strong>有 {{ persistence.lost }} 条调用记录连补写文件都没写成功(已丢失)</strong>,需人工关注。
      </p>
      <p
        v-else-if="persistence && persistence.pending + persistence.claimed + persistence.spilled > 0"
        class="mt__note"
      >
        待补写 {{ persistence.pending }} 条(另有 {{ persistence.claimed }} 条正在写入、已落盘
        {{ persistence.spilled }} 条):数据库恢复后会自动补齐,不阻塞业务。
      </p>
      <p v-if="persistence && persistence.price_error" class="mt__note mt__note--warn">
        <strong>价格表载入失败:</strong>{{ persistence.price_error }}金额会按「无法估算」处理。
      </p>
      <p v-else-if="pricesMissing && totals && totals.calls > 0" class="mt__note">
        还没有配置价格:金额一律显示「无法估算」而不是 0 元。在下方「估算依据」里按
        「服务 / 供应商 / 模型或 OCR Type / 计费单位」添加价目(附官方来源与核实日期);
        只对之后发生的调用生效 —— 历史事件按发生时的快照估算,不会追补重算。
      </p>

      <!-- 概览:实际调用 / 成功失败 / 缓存命中 / 估算费用 -->
      <div class="mt__cards" :class="{ 'is-loading': summaryLoading }" :aria-busy="summaryLoading">
        <div class="card">
          <span class="card__k">实际调用</span>
          <span class="card__v">{{ summaryReady ? totals?.calls : "—" }}</span>
          <span class="card__x">
            条记录 / {{ totals?.http_attempts ?? 0 }} 次真实请求(含 SDK 内部重试)
          </span>
        </div>
        <div class="card" :class="{ 'card--bad': (totals?.failure ?? 0) > 0 }">
          <span class="card__k">失败调用</span>
          <span class="card__v">{{ summaryReady ? totals?.failure : "—" }}</span>
          <span class="card__x">成功 {{ summaryReady ? totals?.success : "—" }} 次</span>
        </div>
        <div class="card">
          <span class="card__k">缓存命中</span>
          <span class="card__v">{{ summaryReady ? cacheHits : "—" }}</span>
          <span class="card__x">
            未命中 {{ cacheMiss }} · 等待复用 {{ cacheShared }}
          </span>
        </div>
        <div class="card" :class="{ 'card--bad': (totals?.unknown_cost ?? 0) > 0 }">
          <span class="card__k">估算费用</span>
          <template v-if="summaryReady && costRows.length">
            <span v-for="c in costRows" :key="c.service + c.currency" class="card__v card__v--small">
              {{ c.currency }} {{ trimDecimal(c.amount) }}
              <span class="dt__hint">{{ SERVICE_TEXT[c.service] ?? c.service }} · {{ c.events }} 条</span>
            </span>
          </template>
          <span v-else class="card__v card__v--small mt__dim">—</span>
          <span class="card__x">
            估算,非官方账单;不同币种不合并。
            <span v-if="(totals?.unknown_cost ?? 0) > 0" class="mt__unknown">
              另有 {{ totals?.unknown_cost }} 条无法估算(缺用量或缺价格)。
            </span>
          </span>
        </div>
      </div>

      <!-- 窗口说明:后端按 UTC 边界取数,这里按本地时区显示,免得对不上账 -->
      <p v-if="windowSince && windowUntil" class="mt__sub">
        已应用范围（本地时间）：{{ formatStampFull(windowSince) }} — {{ formatStampFull(windowUntil) }}
        <span class="mt__dim">不含结束时刻</span>
      </p>

      <nav class="mt__tabs" aria-label="费用视图">
        <button :class="{ 'is-active': activeTab === 'logs' }" :aria-pressed="activeTab === 'logs'" @click="activeTab = 'logs'">调用日志 <span>{{ total }}</span></button>
        <button :class="{ 'is-active': activeTab === 'analysis' }" :aria-pressed="activeTab === 'analysis'" @click="activeTab = 'analysis'">统计分析</button>
      </nav>
      <div v-if="activeTab === 'analysis' && !summaryReady" class="mt__state">{{ summaryLoading ? '正在加载统计…' : '统计暂不可用，请检查上方提示。' }}</div>
      <div v-else-if="activeTab === 'analysis' && !serviceRows.length && !cacheRows.length" class="mt__state">当前范围暂无统计数据，可扩大时间范围后重试。</div>
      <div v-else-if="activeTab === 'analysis'" class="mt__panels">
        <section class="panel">
          <div class="panel__hd">
            <h3 class="panel__t">按服务</h3>
            <span class="panel__hint">记录数 / 失败 / 真实请求</span>
          </div>
          <div class="panel__body">
            <p v-if="!serviceRows.length" class="panel__none">该窗口内没有调用记录。</p>
            <div v-for="s in serviceRows" :key="s.service" class="kv">
              <span class="kv__k">
                {{ s.service }}
                <small>{{ SERVICE_TEXT[s.service] ?? "" }}</small>
              </span>
              <span class="kv__v">
                {{ s.calls }} / {{ s.failure }} / {{ s.http_attempts }}
                <span v-if="s.unknown_cost" class="mt__unknown">({{ s.unknown_cost }} 未知费用)</span>
              </span>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="panel__hd">
            <h3 class="panel__t">用量与估算费用</h3>
            <span class="panel__hint">不同单位 / 币种不合并</span>
          </div>
          <div class="panel__body">
            <p v-if="!usageRows.length && !costRows.length" class="panel__none">
              没有可汇总的用量或金额。
            </p>
            <div v-for="u in usageRows" :key="u.service + u.unit" class="kv">
              <span class="kv__k">
                {{ SERVICE_TEXT[u.service] ?? u.service }}
                <small>用量</small>
              </span>
              <span class="kv__v">{{ trimDecimal(u.quantity, 0) }} {{ u.unit }}</span>
            </div>
            <div v-for="c in costRows" :key="c.service + c.currency" class="kv">
              <span class="kv__k">
                {{ SERVICE_TEXT[c.service] ?? c.service }}
                <small>估算费用</small>
              </span>
              <span class="kv__v">{{ c.currency }} {{ trimDecimal(c.amount) }}</span>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="panel__hd">
            <h3 class="panel__t">缓存(三层分开)</h3>
            <span class="panel__hint">命中 / 未命中 / 复用 / 去重</span>
          </div>
          <div class="panel__body">
            <p v-if="!cacheRows.length" class="panel__none">该窗口内没有缓存统计记录。</p>
            <div v-for="c in cacheRows" :key="c.layer" class="kv">
              <span class="kv__k">
                {{ c.layer }}
                <small>{{ LAYER_TEXT[c.layer] ?? "" }}</small>
              </span>
              <span class="kv__v">
                {{ c.hit }} / {{ c.miss }} / {{ c.shared }} / {{ c.skipped }}
              </span>
            </div>
            <p class="panel__none">
              命中只说明少了一次外部调用或一次本地转换;不换算成「省了多少钱」(价格与命中粒度对不上)。
            </p>
          </div>
        </section>

        <section class="panel">
          <div class="panel__hd">
            <h3 class="panel__t">按天(UTC 日期)</h3>
            <span class="panel__hint">最近 {{ Math.min(DAYS_SHOWN, dayRows.length) }} 天</span>
          </div>
          <div class="panel__body">
            <p v-if="!dayRows.length" class="panel__none">该窗口内没有调用记录。</p>
            <div v-for="d in dayRows" :key="d.day" class="bar">
              <span class="bar__day">{{ d.day.slice(5) }}</span>
              <span class="bar__track">
                <span
                  class="bar__fill"
                  :class="{ 'bar__fill--bad': d.failure > 0 }"
                  :style="{ width: Math.max(2, (d.calls / dayMax) * 100) + '%' }"
                />
              </span>
              <span class="bar__val">{{ d.calls }}<template v-if="d.failure"> ({{ d.failure }} 失败)</template></span>
            </div>
          </div>
        </section>
      </div>

      <!-- 调用日志 -->
      <section v-if="activeTab === 'logs'" class="mt__list" :aria-busy="loading">
        <div class="mt__listHd">
          <h3 class="mt__listT">调用日志</h3>
          <span class="mt__listHint">
            {{ loading ? "正在更新…" : "按时间倒序 · 点击记录查看详情" }}
          </span>
        </div>

        <div v-if="loading && !calls.length" class="mt__state mt__state--soft">
          <p class="mt__stateHd">载入中…</p>
          <p class="mt__stateBody">正在读取调用日志。</p>
        </div>

        <div v-else-if="error" class="mt__state">
          <p class="mt__stateHd">读取失败</p>
          <p class="mt__stateBody">{{ error }}</p>
          <button class="mt__retry" type="button" @click="refresh">重试</button>
        </div>

        <div v-else-if="!calls.length" class="mt__state mt__state--soft">
          <p class="mt__stateHd">这段时间没有调用记录</p>
          <p class="mt__stateBody">
            <template v-if="filtersActive">
              可能是筛选条件太窄(时间范围 / 服务 / 用途 / 状态),换个范围再试。
            </template>
            <template v-else>
              当前范围暂无外部调用记录，缓存命中可在统计分析中查看。
            </template>
          </p>
          <div><button class="mt__retry" @click="setRange('30d')">查看最近 30 天</button><button v-if="filtersActive" class="mt__textBtn" @click="resetFilters">重置筛选</button></div>
        </div>

        <template v-else>
          <div class="mt__scroll" :class="{ 'is-loading': loading }">
            <table class="mt__table">
              <thead>
                <tr>
                  <th>时间(本地)</th>
                  <th>服务 / 用途</th>
                  <th>目标</th>
                  <th>状态</th>
                  <th>耗时</th>
                  <th>用量</th>
                  <th>估算费用</th>
                  <th>归属</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in calls"
                  :key="row.event_id"
                  class="mt__row"
                  tabindex="0"
                  :title="'查看详情:' + row.event_id"
                  @click="openDetail(row.event_id)"
                  @keydown="onRowKey($event, row.event_id)"
                >
                  <td class="nowrap mono" :title="formatStampFull(row.occurred_at)">
                    {{ formatStamp(row.occurred_at) }}
                  </td>
                  <td class="nowrap">
                    {{ row.service }}
                    <span class="mt__dim">{{ PURPOSE_TEXT[row.purpose] ?? row.purpose }}</span>
                  </td>
                  <td :title="row.endpoint">
                    <span class="mono">{{ row.target || "—" }}</span>
                    <span class="mt__dim"> · {{ row.provider }}</span>
                  </td>
                  <td class="nowrap">
                    <span class="tag" :class="row.status === 'failure' ? 'tag--bad' : 'tag--ok'">
                      {{ row.status === "failure" ? "失败" : "成功" }}
                    </span>
                    <span v-if="row.http_status" class="mt__dim mono"> {{ row.http_status }}</span>
                  </td>
                  <td class="nowrap mono">
                    {{ formatMs(row.duration_ms) }}
                    <span v-if="row.http_attempts > 1" class="mt__unknown">
                      ×{{ row.http_attempts }}
                    </span>
                  </td>
                  <td class="nowrap mono">
                    <template v-if="row.usage_quantity !== null">
                      {{ trimDecimal(row.usage_quantity, 0) }} {{ row.usage_unit }}
                    </template>
                    <template v-else><span class="mt__unknown">未取得</span></template>
                  </td>
                  <td class="nowrap">
                    <template v-if="row.cost_amount !== null">
                      <span class="mono">{{ row.currency }} {{ trimDecimal(row.cost_amount) }}</span>
                      <span class="tag tag--est">估算</span>
                    </template>
                    <template v-else>
                      <span class="tag tag--warn" :title="row.cost_note">无法估算</span>
                    </template>
                  </td>
                  <td class="mt__file" :title="row.oss_key || row.job_id || ''">
                    <template v-if="row.oss_key">
                      {{ basename(row.oss_key) }}
                      <span v-if="row.page_no !== null" class="mt__dim">p{{ row.page_no }}</span>
                    </template>
                    <template v-else-if="row.job_id">
                      <span class="mono">任务 {{ row.job_id.slice(0, 8) }}</span>
                    </template>
                    <template v-else>
                      <span class="mt__dim">
                        {{ row.purpose === "query" ? "检索请求" : "未归属文件" }}
                      </span>
                    </template>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="mt__pager">
            <span class="mt__count">
              第 {{ firstIndex }}–{{ lastIndex }} 条 / 共 {{ total }} 条(每页 {{ calls.length }} 条上限 50)
            </span>
            <div class="mt__pagerBtns">
              <button class="mt__btn" type="button" :disabled="!hasPrev || loading" @click="prevPage">
                上一页
              </button>
              <button class="mt__btn" type="button" :disabled="!hasNext || loading" @click="nextPage">
                下一页
              </button>
            </div>
          </div>
        </template>
      </section>

      <details class="mt__details mt__help"><summary>统计说明</summary><div class="mt__detailsBody"><p class="mt__sub">仅统计 Embedding 与 OCR，不含聊天 LLM。金额按调用时的价格快照估算，不是官方账单，不扣减免费额度或折扣；不同币种不合并。缓存命中包含外部结果复用和本地文本转换，不等同于节省的请求数。日期筛选使用本地时间，按天分组使用 UTC 日期。</p></div></details>
      <!-- 价目依据(估算的解释权在配置里;只增 + 删,改价 = 追加更晚生效的规则) -->
      <details class="mt__details">
        <summary>估算依据:当前价目表({{ prices.length }} 条)</summary>
        <div class="mt__detailsBody">
          <div class="mt__priceHd">
            <p class="mt__sub">
              价目按「服务 / 供应商 / 模型或 OCR Type / 计费单位 / 币种」匹配,以生效时间最晚的一条为准。
              改价请添加更晚生效的新规则(不覆盖历史);删除只影响之后的调用。
            </p>
            <button v-if="!priceFormOpen" class="mt__add" type="button" @click="openPriceForm">
              ＋ 添加价目
            </button>
          </div>

          <!-- 新增表单(只增;单价一律按官方价格页核实后填入) -->
          <form v-if="priceFormOpen" class="mt__form" @submit.prevent="submitPrice">
            <p class="mt__formtitle">新增价目</p>

            <div class="mt__field">
              <span class="flt__label">服务</span>
              <div class="flt__chips">
                <button
                  v-for="s in SERVICE_OPTS"
                  :key="s.key"
                  class="flt__chip"
                  :class="{ 'is-on': priceForm.service === s.key }"
                  type="button"
                  @click="priceForm.service = s.key"
                >
                  {{ s.label }}
                </button>
              </div>
            </div>

            <div class="mt__grid">
              <div class="mt__field">
                <label class="flt__label" for="pf-provider">供应商</label>
                <input
                  id="pf-provider"
                  v-model="priceForm.provider"
                  class="mt__input"
                  placeholder="dashscope"
                />
              </div>
              <div class="mt__field">
                <label class="flt__label" for="pf-target">模型 / OCR Type</label>
                <input
                  id="pf-target"
                  v-model="priceForm.target"
                  class="mt__input"
                  placeholder="如 text-embedding-v4;留空 = 该服务通用"
                />
              </div>
            </div>

            <div class="mt__field">
              <span class="flt__label">计费单位</span>
              <div class="flt__chips">
                <button
                  v-for="u in UNIT_OPTS"
                  :key="u.key"
                  class="flt__chip"
                  :class="{ 'is-on': priceForm.unit === u.key }"
                  type="button"
                  :title="u.title"
                  @click="priceForm.unit = u.key"
                >
                  {{ u.label }}
                </button>
              </div>
            </div>

            <div class="mt__grid">
              <div class="mt__field">
                <label class="flt__label" for="pf-cur">币种</label>
                <input
                  id="pf-cur"
                  v-model="priceForm.currency"
                  class="mt__input mt__input--cur"
                  maxlength="3"
                  placeholder="CNY"
                  @input="priceForm.currency = priceForm.currency.toUpperCase()"
                />
              </div>
              <div class="mt__field">
                <label class="flt__label" for="pf-price">单价(按计费单位)</label>
                <input
                  id="pf-price"
                  v-model="priceForm.unit_price"
                  class="mt__input"
                  inputmode="decimal"
                  placeholder="如 0.000514"
                />
              </div>
            </div>

            <div class="mt__field">
              <span class="flt__label">生效时间(本地)</span>
              <div class="mt__rangeRow">
                <DateField v-model="priceDate" overlay />
                <input
                  v-model="priceTime"
                  class="mt__input mt__input--time"
                  type="time"
                  aria-label="生效时刻"
                />
              </div>
            </div>

            <div class="mt__field">
              <label class="flt__label" for="pf-src">来源(官方价格页 + 核实日期)</label>
              <input
                id="pf-src"
                v-model="priceForm.source"
                class="mt__input"
                placeholder="官方价格页 URL 或名称(核实于 2026-10-04)"
              />
            </div>

            <div class="mt__field">
              <label class="flt__label" for="pf-note">备注(可选)</label>
              <input
                id="pf-note"
                v-model="priceForm.note"
                class="mt__input"
                placeholder="如:标准价,未计免费额度 / 折扣"
              />
            </div>

            <p v-if="priceFormError || priceError" class="mt__err">{{ priceFormError || priceError }}</p>

            <div class="mt__formact">
              <button class="mt__save" type="submit" :disabled="priceSaving">
                {{ priceSaving ? "提交中…" : "添加价目" }}
              </button>
              <button class="mt__cancel" type="button" @click="closePriceForm">取消</button>
            </div>
          </form>
          <p v-else-if="priceError" class="mt__err">{{ priceError }}</p>

          <p v-if="pricesError" class="mt__note mt__note--warn">
            {{ pricesError }}
            <button class="mt__retry mt__retry--inline" type="button" @click="loadPrices">
              重新载入
            </button>
          </p>
          <p v-else-if="!prices.length" class="mt__stateBody mt__empty">
            还没有配置价格:金额一律显示「无法估算」。点上方「＋ 添加价目」,按官方价格页填入
            服务 / 供应商 / 模型或 OCR Type / 计费单位 与单价。
          </p>
          <template v-else>
            <table class="mt__priceTable">
              <thead>
                <tr>
                  <th>服务</th>
                  <th>供应商</th>
                  <th>模型 / Type</th>
                  <th>计费单位</th>
                  <th>单价</th>
                  <th>生效时间(本地)</th>
                  <th>来源</th>
                  <th class="mt__opsCol">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(p, i) in prices" :key="p.id ?? i" class="mt__priceRow">
                  <td>{{ p.service }}</td>
                  <td>{{ p.provider }}</td>
                  <td>{{ p.target || "(通用)" }}</td>
                  <td>{{ p.unit }}</td>
                  <td class="mono">{{ p.currency }} {{ trimDecimal(p.unit_price) }}</td>
                  <td class="mono">{{ formatStampFull(p.effective_from) }}</td>
                  <td :title="p.note">{{ p.source || "—" }}</td>
                  <td class="mt__ops">
                    <span class="mt__opsIn">
                      <template v-if="p.id !== null && confirmDel === p.id">
                        <button
                          class="mt__delYes"
                          type="button"
                          :disabled="priceSaving"
                          @click="onRemovePrice(p.id)"
                        >
                          {{ priceSaving ? "删除中…" : "删除" }}
                        </button>
                        <button class="mt__delNo" type="button" @click="confirmDel = null">取消</button>
                      </template>
                      <button
                        v-else-if="p.id !== null"
                        class="mt__delX"
                        type="button"
                        :aria-label="'删除这条价目(id ' + p.id + ')'"
                        title="删除这条价目(历史金额不受影响)"
                        @click="confirmDel = p.id"
                      >
                        ✕
                      </button>
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
            <p class="mt__sub">
              每条调用在发生时打一份价目快照:改价、删价都不会重算历史金额(历史按当时的规则估算)。
            </p>
          </template>
        </div>
      </details>
    </div>

    <!-- 详情抽屉 -->
    <MeteringDetail v-if="detailId" />
  </main>
</template>
