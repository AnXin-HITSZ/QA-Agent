<script setup lang="ts">
// 索引任务与版本:进度(202 + 轮询)、文件明细、失败重试与版本回退。
// 批量重建的入口已从这里移除 —— 一次重建可能跨多种提取方式,一个手动选择器表达不了;
// 计划改为给每个索引任务记提取方式,重建时按任务原方式整批复用。任务的起点现在只有
// 上传(工具条),这里展示进度与结果,并允许按当前提取方式重试失败的文件。
import { computed, ref, watch } from "vue";

import type { IndexJobFileRow, JobPageStats } from "../api";
import { useKnowledge } from "../composables/useKnowledge";
import { errorText, formatStampFull } from "../lib/format";

const {
  job,
  jobFiles,
  jobFilesTotal,
  jobStarting,
  jobError,
  jobRunning,
  manifest,
  modeChosen,
  modeLabel,
  startJob,
  retryJobFile,
  rollbackToPrevious,
  refreshManifest,
} = useKnowledge();

// 展开详情 + 二次确认(回退会切换检索用的版本,先确认)。
const open = ref(false);
const confirmingRollback = ref(false);
const rollbackErr = ref("");
// 回退成功后的提示(切到了哪个版本)。没有它,回退只表现为版本行两个名字对调,看不出做没做。
const rollbackOk = ref("");
// 明细默认只渲染前若干行,大任务按需展开。
const visible = ref(40);

// 一有新任务开始,上次回退的提示就过期(发布后生效版本会再变),先清掉。
watch(jobRunning, (running) => {
  if (running) rollbackOk.value = "";
});

// 最近几条发布 / 回退记录:manifest 给的是旧→新,面板按新→旧展示(最近一次在最上)。
const history = computed(() => [...(manifest.value?.history ?? [])].reverse());

function actionText(a: string): string {
  if (a === "publish") return "发布";
  if (a === "rollback") return "回退";
  return a || "—";
}

const statusText = computed(() => {
  switch (job.value?.status) {
    case "queued":
      return "排队中";
    case "running":
      return "进行中";
    case "published":
      return "已发布";
    case "failed":
      return "未发布";
    default:
      return "";
  }
});

const pct = computed(() => {
  const p = job.value?.progress;
  if (!p?.total) return 0;
  return Math.min(100, Math.round((p.done / p.total) * 100));
});

// 页统计:结束后以后端汇总为准;进行中按已拉到的明细即时累加。
const pageStats = computed<JobPageStats | null>(() => {
  const j = job.value;
  if (!j) return null;
  if (j.summary?.pages) return j.summary.pages;
  if (!jobFiles.value.length) return null;
  const acc: JobPageStats = { total: 0, ok: 0, blank: 0, failed: 0, cached: 0 };
  for (const f of jobFiles.value) {
    acc.total += f.pages?.total ?? 0;
    acc.ok += f.pages?.ok ?? 0;
    acc.blank += f.pages?.blank ?? 0;
    acc.failed += f.pages?.failed ?? 0;
    acc.cached += f.pages?.cached ?? 0;
  }
  return acc;
});

const failedRows = computed(() => jobFiles.value.filter((f) => f.status === "failed"));
const shownRows = computed(() => jobFiles.value.slice(0, visible.value));

// 汇总里的计数(进行中先用明细估算)。
const counts = computed(() => {
  const s = job.value?.summary;
  if (s) {
    return {
      total: s.total_files ?? 0,
      indexed: s.indexed_files ?? 0,
      skipped: s.skipped_files ?? 0,
      failed: s.failed_files ?? 0,
      vectors: s.vectors ?? 0,
    };
  }
  let indexed = 0;
  let skipped = 0;
  let failed = 0;
  for (const f of jobFiles.value) {
    if (f.status === "indexed") indexed += 1;
    else if (f.status === "skipped") skipped += 1;
    else if (f.status === "failed") failed += 1;
  }
  return { total: job.value?.progress.total ?? 0, indexed, skipped, failed, vectors: 0 };
});

// 行内「方式」:原生文本层 / OCR(附本次任务的提取方式)。
function methodText(m: string): string {
  if (!m) return "—";
  if (m.includes("ocr")) {
    const mode = job.value?.options?.extraction_mode;
    return mode && mode !== "native_only" ? `OCR · ${modeLabel(mode)}` : "OCR";
  }
  return "原生文本";
}

function statusOf(r: IndexJobFileRow): { text: string; tone: "ok" | "bad" | "muted" } {
  if (r.status === "indexed") return { text: `已入库 ${r.chunks} 块`, tone: "ok" };
  if (r.status === "failed") return { text: "失败", tone: "bad" };
  return { text: "跳过", tone: "muted" };
}

// 只用当前提取方式重试这批失败文件(显式 key 清单,不再重跑整个范围)。
async function retryFailed(): Promise<void> {
  const keys = failedRows.value.map((f) => f.key);
  if (!keys.length) return;
  await startJob({ kind: "keys", keys });
}

async function doRollback(): Promise<void> {
  confirmingRollback.value = false;
  rollbackErr.value = "";
  rollbackOk.value = "";
  try {
    await rollbackToPrevious();
    rollbackOk.value = manifest.value?.active ?? "";
  } catch (e) {
    rollbackErr.value = errorText(e);
  }
}

function toggle(): void {
  open.value = !open.value;
  if (open.value) void refreshManifest();
}
</script>

<template>
  <section class="rb">
    <button class="rb__toggle" type="button" @click="toggle">
      <span class="rb__caret" :class="{ 'is-open': open }" aria-hidden="true">▸</span>
      索引任务与维护
      <span v-if="jobRunning" class="rb__live">
        进行中 {{ job?.progress.done }}/{{ job?.progress.total }}
      </span>
      <span v-else-if="job && job.status === 'failed'" class="rb__live is-bad">上次未发布</span>
      <span v-else-if="job && job.status === 'published'" class="rb__live is-ok">上次已发布</span>
    </button>

    <div v-if="open" class="rb__body">
      <p v-if="jobError" class="rb__err" role="alert">{{ jobError }}</p>

      <!-- 任务进度与结果 -->
      <div v-if="job" class="rb__job">
        <p class="rb__line">
          <strong>{{ statusText }}</strong>
          · {{ job.progress.done }}/{{ job.progress.total }} 个文件
          <span v-if="job.progress.current">· 当前:{{ job.progress.current }}</span>
        </p>
        <div class="rb__bar" aria-hidden="true">
          <div class="rb__fill" :style="{ width: pct + '%' }"></div>
        </div>

        <p class="rb__line">
          提取方式:{{ modeLabel(job.options?.extraction_mode) }}
          <template v-if="job.options?.mixed_invoice">· 混贴票据页</template>
          <template v-if="job.options?.refresh_ocr">· 忽略缓存重新识别</template>
        </p>
        <p v-if="pageStats && pageStats.total" class="rb__line">
          页进度:{{ pageStats.ok }}/{{ pageStats.total }} 成功 · 空白 {{ pageStats.blank }} · 失败
          {{ pageStats.failed }} · 缓存命中 {{ pageStats.cached }} 页(未重复计费)
        </p>
        <p class="rb__line">
          文件:入库 {{ counts.indexed }} · 跳过 {{ counts.skipped }} · 失败 {{ counts.failed }}
          <span v-if="counts.vectors">· 写入 {{ counts.vectors }} 个向量</span>
        </p>
        <p v-if="job.summary" class="rb__line">
          版本:{{ job.summary.index_version || "—" }}
          <template v-if="job.summary.previous">(发布前 {{ job.summary.previous }})</template>
        </p>
        <p
          v-if="job.status === 'failed' && !jobRunning"
          class="rb__note rb__note--warn"
        >
          本次未发布,{{ job.summary?.message || job.error || "有文件未完成" }}。
          当前仍检索旧索引{{ manifest?.active ? `(${manifest.active})` : "" }},可修好后重试。
        </p>
        <p v-else-if="job.status === 'published' && job.summary?.message" class="rb__note">
          {{ job.summary.message }}
          <template v-if="job.summary.previous">,旧版本 {{ job.summary.previous }} 保留可回退。</template>
        </p>

        <div class="rb__actions">
          <button
            v-if="!jobRunning && failedRows.length"
            class="rb__run"
            type="button"
            :disabled="!modeChosen || jobStarting"
            @click="retryFailed"
          >
            用当前方式重试 {{ failedRows.length }} 个失败文件
          </button>
        </div>

        <!-- 文件明细 -->
        <div v-if="jobFiles.length" class="rb__files">
          <p class="rb__filehead">
            文件明细({{ jobFiles.length }}/{{ jobFilesTotal }})
          </p>
          <table class="rb__table">
            <thead>
              <tr>
                <th>文件</th>
                <th>结果</th>
                <th>方式</th>
                <th>页</th>
                <th>说明</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in shownRows" :key="r.key">
                <td class="rb__key" :title="r.key">{{ r.key }}</td>
                <td :class="'is-' + statusOf(r).tone">{{ statusOf(r).text }}</td>
                <td>{{ methodText(r.method) }}</td>
                <td class="rb__pages">
                  <template v-if="r.pages?.total">
                    {{ r.pages.ok }}/{{ r.pages.total }}
                    <span v-if="r.pages.cached" class="rb__cached">(缓存 {{ r.pages.cached }})</span>
                  </template>
                  <template v-else>—</template>
                </td>
                <td class="rb__reason">
                  {{ r.reason || "—" }}
                  <span v-if="r.failed_pages?.length" class="rb__badpages">
                    失败页:{{ r.failed_pages.join(", ") }}
                  </span>
                </td>
                <td>
                  <button
                    v-if="r.status !== 'indexed' && !jobRunning"
                    class="rb__retry"
                    type="button"
                    :disabled="!modeChosen || jobStarting"
                    title="用当前提取方式单独重试这个文件(会新建一个只含它的任务,明细随之切换)"
                    @click="retryJobFile(r)"
                  >
                    重试
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
          <button
            v-if="jobFiles.length < jobFilesTotal || visible < jobFiles.length"
            class="rb__more"
            type="button"
            @click="visible += 60"
          >
            加载更多
          </button>
        </div>
      </div>

      <!-- 版本与回退 -->
      <div v-if="manifest" class="rb__versions">
        <p class="rb__line">
          当前生效版本:{{ manifest.active }}
          <template v-if="manifest.previous">· 上一版本 {{ manifest.previous }}</template>
        </p>
        <div class="rb__actions">
          <template v-if="confirmingRollback && manifest.previous">
            <span class="rb__ask">回退到上一版本 {{ manifest.previous }}?</span>
            <button class="rb__yes" type="button" @click="doRollback">确认回退</button>
            <button class="rb__no" type="button" @click="confirmingRollback = false">取消</button>
          </template>
          <button
            v-else-if="manifest.previous"
            class="rb__run"
            type="button"
            :disabled="jobRunning"
            :title="jobRunning ? '有任务在运行时不能回退' : ''"
            @click="confirmingRollback = true"
          >
            回退到上一版本
          </button>
        </div>
        <p v-if="rollbackErr" class="rb__err" role="alert">{{ rollbackErr }}</p>
        <p v-else-if="rollbackOk" class="rb__ok" role="status">已切回版本 {{ rollbackOk }}</p>

        <!-- 最近记录:发布 / 回退都留痕,新→旧;排查「什么时候切的、切到哪」看这里 -->
        <div v-if="history.length" class="rb__history">
          <p class="rb__hishead">最近记录(新→旧)</p>
          <ul class="rb__hislist">
            <li v-for="(h, i) in history" :key="i" :title="h.note">
              <span class="rb__histime">{{ formatStampFull(h.at) }}</span>
              <span class="rb__hisact" :class="{ 'is-rollback': h.action === 'rollback' }">
                {{ actionText(h.action) }}
              </span>
              <span class="rb__hisname">{{ h.name }}</span>
              <span v-if="h.replaced" class="rb__hisfrom">← {{ h.replaced }}</span>
              <span v-if="h.points !== null" class="rb__hispts">{{ h.points }} 点</span>
            </li>
          </ul>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.rb {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--line);
}
.rb__toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 0;
  background: transparent;
  color: var(--muted);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
}
.rb__toggle:hover {
  color: var(--ink);
}
.rb__caret {
  display: inline-block;
  transition: transform 0.15s;
  font-size: 10px;
}
.rb__caret.is-open {
  transform: rotate(90deg);
}
.rb__live {
  margin-left: 4px;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--primary-tint);
  color: var(--primary-strong);
  font-size: 11.5px;
}
.rb__live.is-bad {
  background: var(--seal-tint);
  color: var(--seal);
}
.rb__live.is-ok {
  background: var(--primary-tint);
  color: var(--primary-strong);
}
.rb__body {
  margin-top: 10px;
}
.rb__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 10px;
}
.rb__ask {
  color: var(--ink);
  font-size: 13px;
}
.rb__run,
.rb__yes,
.rb__no {
  padding: 6px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.rb__run:hover:not(:disabled) {
  border-color: var(--primary);
  color: var(--primary);
}
.rb__yes {
  border-color: color-mix(in srgb, var(--seal) 42%, transparent);
  background: var(--seal-tint);
  color: var(--seal);
}
.rb__yes:hover:not(:disabled) {
  border-color: var(--seal);
}
.rb__no:hover:not(:disabled) {
  border-color: var(--primary);
  color: var(--primary);
}
.rb__run:disabled,
.rb__yes:disabled,
.rb__no:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.rb__err {
  margin: 12px 0 0;
  color: var(--seal);
  font-size: 13px;
}
.rb__ok {
  margin: 12px 0 0;
  color: var(--primary-strong);
  font-size: 13px;
}
.rb__history {
  margin-top: 12px;
}
.rb__hishead {
  margin: 0 0 6px;
  color: var(--muted);
  font-size: 12px;
}
.rb__hislist {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.rb__hislist li {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}
.rb__histime,
.rb__hisname,
.rb__hisfrom {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11.5px;
}
.rb__histime {
  white-space: nowrap;
}
.rb__hisact {
  padding: 0 7px;
  border-radius: 999px;
  background: var(--primary-tint);
  color: var(--primary-strong);
  font-size: 11.5px;
  line-height: 1.7;
}
.rb__hisact.is-rollback {
  background: var(--seal-tint);
  color: var(--seal);
}
.rb__hisname {
  color: var(--ink);
}
.rb__hisfrom {
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 320px;
  white-space: nowrap;
}
.rb__hispts {
  white-space: nowrap;
}
.rb__job {
  margin-top: 12px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
}
.rb__line {
  margin: 0 0 6px;
  color: var(--muted);
  font-size: 12.5px;
  line-height: 1.7;
}
.rb__line strong {
  color: var(--ink);
}
.rb__bar {
  height: 6px;
  margin: 4px 0 8px;
  border-radius: 999px;
  background: var(--line);
  overflow: hidden;
}
.rb__fill {
  height: 100%;
  border-radius: 999px;
  background: var(--primary);
  transition: width 0.3s;
}
.rb__note {
  margin: 6px 0 0;
  color: var(--muted);
  font-size: 12.5px;
  line-height: 1.7;
}
.rb__note--warn {
  color: var(--seal);
}
.rb__files {
  margin-top: 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  overflow: hidden;
}
.rb__filehead {
  margin: 0;
  padding: 8px 12px;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 12px;
}
.rb__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}
.rb__table th {
  padding: 7px 10px;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-weight: 500;
  text-align: left;
  white-space: nowrap;
}
.rb__table td {
  padding: 7px 10px;
  border-bottom: 1px solid var(--line);
  color: var(--ink);
  vertical-align: top;
}
.rb__table tr:last-child td {
  border-bottom: 0;
}
.rb__key {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rb__table td.is-ok {
  color: var(--primary-strong);
}
.rb__table td.is-bad {
  color: var(--seal);
}
.rb__table td.is-muted {
  color: var(--muted);
}
.rb__pages,
.rb__cached {
  white-space: nowrap;
}
.rb__cached {
  color: var(--muted);
}
.rb__reason {
  color: var(--muted);
  max-width: 280px;
}
.rb__badpages {
  display: block;
  color: var(--seal);
}
.rb__retry {
  height: 22px;
  padding: 0 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-xs);
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.rb__retry:hover:not(:disabled) {
  border-color: var(--primary);
  color: var(--primary);
}
.rb__retry:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.rb__more {
  width: 100%;
  padding: 7px;
  border: 0;
  border-top: 1px solid var(--line);
  background: var(--surface-2);
  color: var(--primary);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.rb__versions {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--line);
}

@media (max-width: 640px) {
  .rb__table th:nth-child(3),
  .rb__table td:nth-child(3) {
    display: none;
  }
}
</style>
