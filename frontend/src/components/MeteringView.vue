<script setup lang="ts">
// 调用与费用:实际外部调用(embedding / OCR)的日志与**估算**费用统计。
//
// 界面口径(与后端接口、docs/调用日志与费用统计技术方案.md 一致,改文案前先改文档):
// - 金额一律标「估算」:按配置单价 × 用量算出来的,不是官方账单,也不扣免费额度 / 折扣;
// - 拿不到用量或缺价格时显示「无法估算」+ 原因,绝不显示 ¥0;
// - 缓存命中与实际调用分列:命中不产生外部调用,也就不产生费用;
// - 业务失败(供应商返回失败 / 超时)与日志补写失败(写库不通)是两回事,分开提示。
import { computed, onActivated, onErrorCaptured, ref } from "vue";

import "../styles/metering.css";
import {
  useMetering,
  type PurposeKey,
  type RangeKey,
  type ServiceKey,
  type StatusKey,
} from "../composables/useMetering";
import { formatMs, formatStamp, formatStampFull, trimDecimal } from "../lib/format";
import MeteringDetail from "./MeteringDetail.vue";

const {
  range,
  customSince,
  customUntil,
  service,
  purpose,
  status,
  summary,
  calls,
  total,
  loading,
  error,
  summaryError,
  prices,
  pricesError,
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

const RANGES: { key: RangeKey; label: string }[] = [
  { key: "24h", label: "最近 24 小时" },
  { key: "7d", label: "最近 7 天" },
  { key: "30d", label: "最近 30 天" },
  { key: "custom", label: "自定义" },
];

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
          <p class="mt__sub">
            实际外部调用的日志与<strong>估算</strong>费用:每次真实请求一条记录,重试逐次记录。
            金额 = 配置单价 × 用量,不是官方账单(官方账单本期未接入);只统计 embedding 与 OCR,
            <strong>不含聊天 LLM 调用</strong>。
          </p>
        </div>
        <button class="btn" type="button" :disabled="loading" @click="refresh">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
      </header>

      <!-- 筛选 -->
      <div class="mt__filters">
        <label class="flt">
          <span class="flt__label">时间范围</span>
          <select
            class="flt__sel"
            :value="range"
            @change="setRange(($event.target as HTMLSelectElement).value as RangeKey)"
          >
            <option v-for="r in RANGES" :key="r.key" :value="r.key">{{ r.label }}</option>
          </select>
        </label>
        <label v-if="range === 'custom'" class="flt">
          <span class="flt__label">起(本地时间)</span>
          <input v-model="customSince" class="flt__in flt__in--dt" type="datetime-local" />
        </label>
        <label v-if="range === 'custom'" class="flt">
          <span class="flt__label">止(不含)</span>
          <input v-model="customUntil" class="flt__in flt__in--dt" type="datetime-local" />
        </label>
        <label class="flt">
          <span class="flt__label">服务</span>
          <select
            class="flt__sel"
            :value="service"
            @change="
              service = ($event.target as HTMLSelectElement).value as ServiceKey;
              applyFilters();
            "
          >
            <option value="">全部</option>
            <option value="embedding">embedding(向量化)</option>
            <option value="ocr">ocr(文字 / 票据识别)</option>
          </select>
        </label>
        <label class="flt">
          <span class="flt__label">用途</span>
          <select
            class="flt__sel"
            :value="purpose"
            @change="
              purpose = ($event.target as HTMLSelectElement).value as PurposeKey;
              applyFilters();
            "
          >
            <option value="">全部</option>
            <option value="document_index">索引(写库)</option>
            <option value="query">检索(问答)</option>
          </select>
        </label>
        <label class="flt">
          <span class="flt__label">状态</span>
          <select
            class="flt__sel"
            :value="status"
            @change="
              status = ($event.target as HTMLSelectElement).value as StatusKey;
              applyFilters();
            "
          >
            <option value="">全部</option>
            <option value="success">成功</option>
            <option value="failure">失败</option>
          </select>
        </label>
        <button
          v-if="range === 'custom'"
          class="btn mt__filterGo"
          type="button"
          @click="applyFilters"
        >
          应用
        </button>
      </div>

      <!-- 口径与健康提示(按重要性依次出现) -->
      <p v-if="persistence && !persistence.enabled" class="mt__note">
        <strong>调用日志与费用统计未启用。</strong
        >{{ persistence.message || "请在 backend/.env 配置 METERING_MYSQL_URL 后重启后端。" }}
        未配置不影响 OCR / 索引 / 检索,只是不留调用记录。
      </p>
      <p v-if="persistence && persistence.enabled && !persistence.auth_configured" class="mt__note mt__note--warn">
        <strong>管理员接口未配置访问令牌。</strong>调用日志与费用数据目前对任何能访问本服务的人可见
        —— 请在 <code>backend/.env</code> 配置 <code>ADMIN_API_TOKEN</code>(并只在受信网络内暴露服务)。
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
        还没有配置价格:所有金额都会显示「无法估算」而不是 0 元。在
        <code>price_config</code> 表里按「服务 / 供应商 / 模型或 OCR Type / 计费单位」配置后生效
        (见技术方案 §5)。
      </p>

      <!-- 概览:实际调用 / 成功失败 / 缓存命中 / 估算费用 -->
      <div class="mt__cards">
        <div class="card">
          <span class="card__k">实际调用(本窗口)</span>
          <span class="card__v">{{ totals?.calls ?? 0 }}</span>
          <span class="card__x">
            条记录 / {{ totals?.http_attempts ?? 0 }} 次真实请求(含 SDK 内部重试)
          </span>
        </div>
        <div class="card" :class="{ 'card--bad': (totals?.failure ?? 0) > 0 }">
          <span class="card__k">成功 / 失败</span>
          <span class="card__v">{{ totals?.success ?? 0 }} / {{ totals?.failure ?? 0 }}</span>
          <span class="card__x">失败是业务调用失败,与日志补写失败无关</span>
        </div>
        <div class="card">
          <span class="card__k">缓存命中(不产生外部调用)</span>
          <span class="card__v">{{ cacheHits }}</span>
          <span class="card__x">
            未命中 {{ cacheMiss }} · 等待复用 {{ cacheShared }} —— 命中单独统计,不计入上面的调用数
          </span>
        </div>
        <div class="card" :class="{ 'card--bad': (totals?.unknown_cost ?? 0) > 0 }">
          <span class="card__k">估算费用</span>
          <template v-if="costRows.length">
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
        统计窗口(本地时间):{{ formatStampFull(windowSince) }} — {{ formatStampFull(windowUntil) }}
        <span class="mt__dim">(不含止点;后端按 UTC 边界取数)</span>
      </p>

      <!-- 分组 -->
      <div class="mt__panels">
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
      <section class="mt__list">
        <div class="mt__listHd">
          <h3 class="mt__listT">调用日志</h3>
          <span class="mt__listHint">
            按开始时间倒序 · 点任意一行看详情(用量来源、价格依据、错误摘要)
          </span>
        </div>

        <div v-if="loading" class="mt__state mt__state--soft">
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
              窗口内确实没有 embedding / OCR 调用(命中缓存不会产生调用记录)。
            </template>
          </p>
        </div>

        <template v-else>
          <div class="mt__scroll">
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
              <button class="btn" type="button" :disabled="!hasPrev || loading" @click="prevPage">
                上一页
              </button>
              <button class="btn" type="button" :disabled="!hasNext || loading" @click="nextPage">
                下一页
              </button>
            </div>
          </div>
        </template>
      </section>

      <!-- 价目依据(估算的解释权在配置里) -->
      <details class="mt__details">
        <summary>估算依据:当前价目表({{ prices.length }} 条)</summary>
        <div class="mt__detailsBody">
          <p v-if="pricesError" class="mt__note mt__note--warn">{{ pricesError }}</p>
          <template v-else-if="!prices.length">
            <p class="mt__stateBody">
              还没有配置价格:金额一律显示「无法估算」。价目由运维写入 MySQL 的
              <code>price_config</code> 表(按 服务 / 供应商 / 模型或 OCR Type / 计费单位 配置,
              引用官方价格来源与核实日期)。
            </p>
            <button class="mt__retry" type="button" @click="loadPrices">重新载入</button>
          </template>
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
                </tr>
              </thead>
              <tbody>
                <tr v-for="(p, i) in prices" :key="p.id ?? i">
                  <td>{{ p.service }}</td>
                  <td>{{ p.provider }}</td>
                  <td>{{ p.target || "(通用)" }}</td>
                  <td>{{ p.unit }}</td>
                  <td class="mono">{{ p.currency }} {{ trimDecimal(p.unit_price) }}</td>
                  <td class="mono">{{ formatStampFull(p.effective_from) }}</td>
                  <td :title="p.note">{{ p.source || "—" }}</td>
                </tr>
              </tbody>
            </table>
            <p class="mt__sub">
              每条调用在发生时打一份价目快照:改价不会重算历史金额(历史按当时的规则估算)。
            </p>
          </template>
        </div>
      </details>
    </div>

    <!-- 详情抽屉 -->
    <MeteringDetail v-if="detailId" />
  </main>
</template>
