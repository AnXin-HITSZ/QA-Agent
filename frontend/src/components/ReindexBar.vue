<script setup lang="ts">
import { ref } from "vue";

import { useKnowledge } from "../composables/useKnowledge";

const { reindexing, reindexResult, runReindex } = useKnowledge();

// 展开维护区 + 二次确认(整库重建会先清空整个索引再重建,不可逆,须确认)。
const open = ref(false);
const confirming = ref(false);
const err = ref("");

async function doReindex(): Promise<void> {
  confirming.value = false;
  err.value = "";
  try {
    await runReindex(""); // 空 = 整库
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e);
  }
}
</script>

<template>
  <section class="rb">
    <button class="rb__toggle" type="button" @click="open = !open">
      <span class="rb__caret" :class="{ 'is-open': open }" aria-hidden="true">▸</span>
      维护
    </button>

    <div v-if="open" class="rb__body">
      <p class="rb__desc">
        全量重建会<strong>清空整个向量索引后按当前 OSS 原件重建</strong>。日常上传 / 删除已自动维护索引,
        这里仅在索引与原件不同步时作兜底。重建期间检索可能短暂查不到结果。
      </p>

      <div class="rb__actions">
        <template v-if="confirming">
          <span class="rb__ask">确认清空并重建整库?</span>
          <button class="rb__yes" type="button" :disabled="reindexing" @click="doReindex">
            确认重建
          </button>
          <button class="rb__no" type="button" :disabled="reindexing" @click="confirming = false">
            取消
          </button>
        </template>
        <button
          v-else
          class="rb__run"
          type="button"
          :disabled="reindexing"
          @click="confirming = true"
        >
          {{ reindexing ? "重建中…" : "全量重建整库" }}
        </button>
      </div>

      <p v-if="err" class="rb__err" role="alert">{{ err }}</p>

      <p v-else-if="reindexResult" class="rb__result">
        重建完成:共 {{ reindexResult.total_files }} 文件,入库
        {{ reindexResult.indexed_files }}、跳过 {{ reindexResult.skipped_files }},写入
        {{ reindexResult.vectors }} 个向量({{ reindexResult.chunks }} 切块)。
        <span v-if="reindexResult.skipped_files">部分文件因需 OCR / 不支持等原因未入库。</span>
      </p>
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
.rb__body {
  margin-top: 10px;
}
.rb__desc {
  margin: 0 0 12px;
  color: var(--muted);
  font-size: 12.5px;
  line-height: 1.7;
}
.rb__desc strong {
  color: var(--ink);
  font-weight: 600;
}
.rb__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
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
.rb__result {
  margin: 12px 0 0;
  color: var(--muted);
  font-size: 12.5px;
  line-height: 1.7;
}
</style>
