<script setup lang="ts">
// 单条调用详情(右侧抽屉):归属、调用链路、用量与计费口径、估算费用与价格依据、错误摘要。
// 口径提示写在字段旁而不是只写文档里:金额一律标「估算」,拿不到就写「无法估算」+ 原因。
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import { useKnowledge } from "../composables/useKnowledge";
import { useMetering } from "../composables/useMetering";
import { formatMs, formatStampFull, trimDecimal } from "../lib/format";

const { detail, detailLoading, detailError, closeDetail } = useMetering();
// 知识库视图的单例:详情里的文件可一键跳到它所在分类(两视图共用同一份位置状态)。
const { goto } = useKnowledge();

const panel = ref<HTMLElement | null>(null);
let lastFocused: HTMLElement | null = null;

// 打开时把焦点请进抽屉,并记住来处;关闭 / 卸载时还回去(键盘用户不会掉到页面顶部)。
onMounted(() => {
  lastFocused = (document.activeElement as HTMLElement | null) ?? null;
  panel.value?.focus({ preventScroll: true });
});
onBeforeUnmount(() => {
  if (lastFocused?.isConnected) lastFocused.focus({ preventScroll: true });
});

// Esc 关闭:除点遮罩 / 关闭按钮之外的另一条出口。
function onKeydown(e: KeyboardEvent): void {
  if (e.key === "Tab") {
    const nodes = panel.value?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), [tabindex="0"]');
    if (nodes?.length) {
      const first = nodes[0]; const last = nodes[nodes.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    } else { e.preventDefault(); }
  }
  if (e.key === "Escape") {
    e.stopPropagation();
    closeDetail();
  }
}

const USAGE_SOURCE: Record<string, string> = {
  vendor_response: "供应商响应里报的用量",
  local_count: "本端计数(按页 / 按请求)",
  unknown: "未知(没取到)",
};
const BILLING: Record<string, string> = {
  billable: "供应商侧应计费",
  unknown: "是否计费未知(如超时无响应)",
};

const purposeText = computed(() => (detail.value?.purpose === "query" ? "检索" : "索引"));
const isFailure = computed(() => detail.value?.status === "failure");
// 检索侧的调用不属于某个文件(没有 job / document):如实说明,而不是留一片空白。
const scopeText = computed(() => (purposeText.value === "索引" ? "索引任务" : "检索请求"));

// oss_key → 所在分类前缀(知识库视图按前缀浏览)。
const folderOf = computed(() => {
  const key = detail.value?.oss_key ?? "";
  const cut = key.lastIndexOf("/");
  return cut >= 0 ? key.slice(0, cut + 1) : "";
});

function goKnowledge(): void {
  goto(folderOf.value); // 先把知识库切到该文件所在分类,再走 hash 路由切视图
  location.hash = "#/knowledge";
}

// 价格快照是「事件发生时刻」的价目原样:改价不改历史,所以这里展示的是当时的依据。
const snapshotText = computed(() => {
  const snap = detail.value?.price_snapshot;
  if (!snap || !Object.keys(snap).length) return "";
  try {
    return JSON.stringify(snap, null, 2);
  } catch {
    return "";
  }
});
</script>

<template>
  <div class="mt__shade" @click="closeDetail" />
  <!-- 焦点容器自身可聚焦(tabindex=-1),打开即把焦点请进来;role=dialog 让读屏知道这是弹层 -->
  <aside
    ref="panel"
    class="dt"
    role="dialog"
    aria-modal="true"
    aria-label="调用详情"
    tabindex="-1"
    @keydown="onKeydown"
  >
    <header class="dt__hd">
      <div>
        <h3 class="dt__title">
          调用详情
          <span v-if="detail" class="tag" :class="isFailure ? 'tag--bad' : 'tag--ok'">
            {{ isFailure ? "失败" : "成功" }}
          </span>
        </h3>
        <p class="dt__subtitle">{{ detail?.event_id ?? "" }}</p>
      </div>
      <button class="dt__close" type="button" aria-label="关闭详情" @click="closeDetail">✕</button>
    </header>

    <div class="dt__body">
      <p v-if="detailLoading" class="dt__note">载入中…</p>
      <p v-else-if="detailError" class="dt__err" role="alert">{{ detailError }}</p>

      <template v-else-if="detail">
        <!-- 概览 -->
        <section class="dt__sec">
          <h4 class="dt__secHd">本次调用</h4>
          <div class="dt__row">
            <span class="dt__k">本地时间</span>
            <span class="dt__v dt__v--mono">{{ formatStampFull(detail.occurred_at) }}</span>
          </div>
          <div class="dt__row">
            <span class="dt__k">UTC</span>
            <span class="dt__v dt__v--mono">{{ detail.occurred_at }}</span>
          </div>
          <div class="dt__row">
            <span class="dt__k">服务 / 用途</span>
            <span class="dt__v">{{ detail.service }} · {{ purposeText }}</span>
          </div>
          <div class="dt__row">
            <span class="dt__k">目标</span>
            <span class="dt__v dt__v--mono">{{ detail.target || "—" }}</span>
          </div>
          <div class="dt__row">
            <span class="dt__k">供应商 / 端点</span>
            <span class="dt__v dt__v--mono">{{ detail.provider }} · {{ detail.endpoint || "—" }}</span>
          </div>
          <div class="dt__row">
            <span class="dt__k">耗时</span>
            <span class="dt__v dt__v--mono">{{ formatMs(detail.duration_ms) }}</span>
          </div>
          <div class="dt__row">
            <span class="dt__k">实际请求次数</span>
            <span class="dt__v">
              {{ detail.http_attempts }} 次
              <span class="dt__hint">(第 {{ detail.attempt_no }} 次逻辑尝试;SDK 内部重试计入)</span>
            </span>
          </div>
        </section>

        <!-- 归属 -->
        <section class="dt__sec">
          <h4 class="dt__secHd">归属({{ scopeText }})</h4>
          <template v-if="detail.oss_key || detail.job_id || detail.document_id">
            <div v-if="detail.oss_key" class="dt__row">
              <span class="dt__k">文件</span>
              <span class="dt__v">{{ detail.oss_key }}</span>
            </div>
            <div v-if="detail.page_no !== null" class="dt__row">
              <span class="dt__k">页码</span>
              <span class="dt__v dt__v--mono">第 {{ detail.page_no }} 页</span>
            </div>
            <div v-if="detail.job_id" class="dt__row">
              <span class="dt__k">索引任务</span>
              <span class="dt__v dt__v--mono">{{ detail.job_id }}</span>
            </div>
            <div v-if="detail.document_id" class="dt__row">
              <span class="dt__k">文件身份</span>
              <span class="dt__v dt__v--mono">{{ detail.document_id }}</span>
            </div>
            <div v-if="detail.oss_key" class="dt__actions">
              <button class="dt__link" type="button" @click="goKnowledge">
                在知识库中查看所在分类
              </button>
            </div>
          </template>
          <p v-else class="dt__note">
            未归属到具体文件 —— 检索侧调用不绑定文件(与索引任务分开统计)。
          </p>
        </section>

        <!-- 用量与计费口径 -->
        <section class="dt__sec">
          <h4 class="dt__secHd">用量(计费口径)</h4>
          <div class="dt__row">
            <span class="dt__k">用量</span>
            <span class="dt__v dt__v--mono">
              <template v-if="detail.usage_quantity !== null">
                {{ trimDecimal(detail.usage_quantity, 0) }} {{ detail.usage_unit }}
              </template>
              <template v-else>未取得</template>
            </span>
          </div>
          <div class="dt__row">
            <span class="dt__k">用量来源</span>
            <span class="dt__v">{{ USAGE_SOURCE[detail.usage_source] ?? detail.usage_source }}</span>
          </div>
          <div class="dt__row">
            <span class="dt__k">计费数量</span>
            <span class="dt__v dt__v--mono">
              <template v-if="detail.billing_quantity !== null">
                {{ trimDecimal(detail.billing_quantity, 0, 8) }} {{ detail.billing_unit }}
              </template>
              <template v-else>未知</template>
            </span>
          </div>
          <div class="dt__row">
            <span class="dt__k">是否计费</span>
            <span class="dt__v" :class="{ 'dt__v--bad': detail.billing_status !== 'billable' }">
              {{ BILLING[detail.billing_status] ?? detail.billing_status }}
            </span>
          </div>
          <p v-if="detail.usage_note" class="dt__note">{{ detail.usage_note }}</p>
          <p v-if="detail.billing_note" class="dt__note">{{ detail.billing_note }}</p>
          <p class="dt__note">
            超时 / 无响应不等于免费:那时记「是否计费未知」,不记 0。
          </p>
        </section>

        <!-- 估算费用 -->
        <section class="dt__sec">
          <h4 class="dt__secHd">费用</h4>
          <div class="dt__row">
            <span class="dt__k">估算费用</span>
            <span class="dt__v">
              <template v-if="detail.cost_amount !== null">
                <span class="dt__v--mono">
                  {{ detail.currency || "" }} {{ trimDecimal(detail.cost_amount) }}
                </span>
                <span class="tag tag--est dt__tag">估算</span>
              </template>
              <template v-else>
                <span class="tag tag--warn">无法估算</span>
              </template>
            </span>
          </div>
          <div v-if="detail.price_version" class="dt__row">
            <span class="dt__k">价目版本</span>
            <span class="dt__v dt__v--mono">{{ detail.price_version }}</span>
          </div>
          <p v-if="detail.cost_note" class="dt__note">{{ detail.cost_note }}</p>
          <p class="dt__note">
            估算金额 = 配置单价 × 用量,不是官方账单(本期未接官方账单,也不扣免费额度 / 折扣)。
          </p>
          <details v-if="snapshotText" class="mt__details">
            <summary>事件发生时的价目快照(不可变)</summary>
            <div class="mt__detailsBody">
              <pre class="dt__snap">{{ snapshotText }}</pre>
            </div>
          </details>
        </section>

        <!-- 按文件分摊 -->
        <section v-if="detail.items && detail.items.length > 1" class="dt__sec">
          <h4 class="dt__secHd">按文件的估算分摊</h4>
          <table class="dt__table">
            <thead>
              <tr>
                <th>文件</th>
                <th>页</th>
                <th>文本数</th>
                <th>分摊金额</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(it, i) in detail.items" :key="i">
                <td>{{ it.oss_key || "—" }}</td>
                <td class="mono">{{ it.page_no ?? "—" }}</td>
                <td class="mono">{{ it.text_count || "—" }}</td>
                <td class="mono">
                  <template v-if="it.allocated_cost !== null">
                    {{ trimDecimal(it.allocated_cost) }}
                  </template>
                  <template v-else>—</template>
                </td>
              </tr>
            </tbody>
          </table>
          <p class="dt__note">
            {{ detail.items[0]?.allocation_note || "分摊为估算值,仅供归因;整笔金额只算一次。" }}
          </p>
        </section>

        <!-- 错误 -->
        <section v-if="isFailure || detail.error_message || detail.error_class" class="dt__sec">
          <h4 class="dt__secHd">错误(业务失败;与日志补写失败是两回事)</h4>
          <div v-if="detail.http_status !== null" class="dt__row">
            <span class="dt__k">HTTP 状态</span>
            <span class="dt__v dt__v--mono dt__v--bad">{{ detail.http_status }}</span>
          </div>
          <div v-if="detail.error_class" class="dt__row">
            <span class="dt__k">异常类型</span>
            <span class="dt__v dt__v--mono">{{ detail.error_class }}</span>
          </div>
          <p v-if="detail.error_message" class="dt__note">{{ detail.error_message }}</p>
          <p class="dt__note">错误摘要与端点已脱敏:不含 API Key、签名参数与文档正文。</p>
        </section>

        <!-- 链路 -->
        <section class="dt__sec">
          <h4 class="dt__secHd">重试链路</h4>
          <div v-if="detail.call_group" class="dt__row">
            <span class="dt__k">调用分组</span>
            <span class="dt__v dt__v--mono">{{ detail.call_group }}</span>
          </div>
          <div v-if="detail.retry_of" class="dt__row">
            <span class="dt__k">上一次尝试</span>
            <span class="dt__v dt__v--mono">{{ detail.retry_of }}</span>
          </div>
          <div v-if="detail.provider_request_id" class="dt__row">
            <span class="dt__k">供应商请求 id</span>
            <span class="dt__v dt__v--mono">{{ detail.provider_request_id }}</span>
          </div>
          <p class="dt__note">每次真实尝试各自记一条,便于核对「重试了几次、分别花了多少」。</p>
        </section>
      </template>
    </div>
  </aside>
</template>
