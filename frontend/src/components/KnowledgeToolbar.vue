<script setup lang="ts">
import { ref } from "vue";

import { useKnowledge, type UploadRow } from "../composables/useKnowledge";

const { uploading, uploadRows, makeFolder, uploadAndIndex, clearUploads } = useKnowledge();

// 新建分类的行内表单状态。
const creating = ref(false);
const folderName = ref("");
const createError = ref("");
const creatingBusy = ref(false);
const nameInput = ref<HTMLInputElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);

function openCreate(): void {
  creating.value = true;
  folderName.value = "";
  createError.value = "";
  void nameInput.value?.focus();
}

function cancelCreate(): void {
  creating.value = false;
  createError.value = "";
}

async function submitCreate(): Promise<void> {
  const name = folderName.value.trim();
  if (!name) {
    createError.value = "请输入分类名。";
    return;
  }
  if (/[/\\]/.test(name) || name === "." || name === "..") {
    createError.value = "分类名不能含「/」「\\」,也不能是「.」或「..」。";
    return;
  }
  creatingBusy.value = true;
  createError.value = "";
  try {
    await makeFolder(name);
    creating.value = false;
  } catch (e) {
    createError.value = e instanceof Error ? e.message : String(e);
  } finally {
    creatingBusy.value = false;
  }
}

function pickFiles(): void {
  fileInput.value?.click();
}

async function onFiles(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement;
  const files = input.files ? Array.from(input.files) : [];
  input.value = ""; // 清空,允许再次选同名文件
  if (files.length) await uploadAndIndex(files);
}

// 进度行 → 展示文案 + 语气(ok 青 / bad 红 / muted 灰 / pending 进行中)。
function phaseText(r: UploadRow): string {
  switch (r.phase) {
    case "uploading":
      return "上传中…";
    case "indexing":
      return "正在索引…";
    case "indexed":
      return r.chunks ? `已索引 · ${r.chunks} 块` : "已索引";
    case "index_failed":
      return `索引失败:${r.reason ?? "未知原因"}`;
    case "skipped_exists":
      return "同名已存在,未覆盖";
    case "rejected":
      return "文件名非法,未接收";
    case "upload_error":
      return `上传失败:${r.reason ?? "未知原因"}`;
    default:
      return "";
  }
}

function phaseTone(r: UploadRow): "ok" | "bad" | "muted" | "pending" {
  switch (r.phase) {
    case "indexed":
      return "ok";
    case "index_failed":
    case "upload_error":
    case "rejected":
      return "bad";
    case "skipped_exists":
      return "muted";
    default:
      return "pending";
  }
}
</script>

<template>
  <div class="tb">
    <div class="tb__actions">
      <button
        class="tb__btn"
        type="button"
        :disabled="creating || uploading"
        @click="openCreate"
      >
        <span aria-hidden="true">＋</span> 新建分类
      </button>
      <button class="tb__btn" type="button" :disabled="uploading" @click="pickFiles">
        <span aria-hidden="true">↑</span> {{ uploading ? "上传中…" : "上传文件" }}
      </button>
      <input
        ref="fileInput"
        class="tb__file"
        type="file"
        multiple
        @change="onFiles"
      />
    </div>

    <form v-if="creating" class="tb__create" @submit.prevent="submitCreate">
      <input
        ref="nameInput"
        v-model="folderName"
        class="tb__input"
        type="text"
        placeholder="分类名,如「差旅报销」"
        :disabled="creatingBusy"
        @keydown.esc="cancelCreate"
      />
      <button class="tb__ok" type="submit" :disabled="creatingBusy">
        {{ creatingBusy ? "创建中…" : "创建" }}
      </button>
      <button class="tb__cancel" type="button" :disabled="creatingBusy" @click="cancelCreate">
        取消
      </button>
    </form>
    <p v-if="createError" class="tb__err" role="alert">{{ createError }}</p>

    <div v-if="uploadRows.length" class="tb__uploads">
      <div class="tb__uphead">
        <span>上传进度</span>
        <button v-if="!uploading" class="tb__clear" type="button" @click="clearUploads">清除</button>
      </div>
      <ul class="tb__uplist">
        <li v-for="(r, i) in uploadRows" :key="i" class="tb__uprow">
          <span class="tb__upname" :title="r.name">{{ r.name }}</span>
          <span class="tb__upstat" :class="'is-' + phaseTone(r)">{{ phaseText(r) }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.tb {
  margin-bottom: 14px;
}
.tb__actions {
  display: flex;
  gap: 8px;
}
.tb__btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  font-size: 13.5px;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.tb__btn:hover:not(:disabled) {
  border-color: var(--primary);
  color: var(--primary);
}
.tb__btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.tb__file {
  display: none;
}

.tb__create {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
.tb__input {
  flex: 1;
  min-width: 0;
  padding: 7px 11px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  font-size: 13.5px;
}
.tb__input:focus {
  outline: 2px solid var(--primary);
  outline-offset: -1px;
  border-color: var(--primary);
}
.tb__ok,
.tb__cancel {
  padding: 7px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  font: inherit;
  font-size: 13.5px;
  cursor: pointer;
  flex-shrink: 0;
}
.tb__ok {
  border-color: var(--primary);
  background: var(--primary-tint);
  color: var(--primary-strong);
}
.tb__ok:hover:not(:disabled) {
  border-color: var(--primary-strong);
}
.tb__cancel {
  background: var(--surface);
  color: var(--ink);
}
.tb__cancel:hover:not(:disabled) {
  border-color: var(--primary);
  color: var(--primary);
}
.tb__ok:disabled,
.tb__cancel:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.tb__err {
  margin: 8px 0 0;
  color: var(--seal);
  font-size: 13px;
}

.tb__uploads {
  margin-top: 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  overflow: hidden;
}
.tb__uphead {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 12px;
}
.tb__clear {
  border: 0;
  background: transparent;
  color: var(--primary);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.tb__clear:hover {
  text-decoration: underline;
}
.tb__uplist {
  margin: 0;
  padding: 0;
  list-style: none;
}
.tb__uprow {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--line);
}
.tb__uprow:last-child {
  border-bottom: 0;
}
.tb__upname {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: var(--ink);
}
.tb__upstat {
  flex-shrink: 0;
  font-size: 12px;
}
.tb__upstat.is-ok {
  color: var(--primary-strong);
}
.tb__upstat.is-bad {
  color: var(--seal);
}
.tb__upstat.is-muted {
  color: var(--muted);
}
.tb__upstat.is-pending {
  color: var(--primary);
}
</style>
