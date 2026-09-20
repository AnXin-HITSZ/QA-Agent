<script setup lang="ts">
import { ref, watch } from "vue";

import type { SopWrite } from "../api";
import { useSops } from "../composables/useSops";
import MarkdownView from "./MarkdownView.vue";

const {
  current,
  editingNew,
  detailLoading,
  detailError,
  saving,
  saveError,
  save,
  remove,
  openDetail,
  showList,
} = useSops();

// 与后端 _ID_RE 一致:字母 / 数字开头,其后可含字母 / 数字 / 下划线 / 连字符。
const ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

// 标准章节骨架(对齐原型 TEMPLATE)。
const TEMPLATE = "## 适用范围\n\n## 所需材料\n\n## 办理步骤\n\n## 注意事项\n";

// 本地表单状态(与单例解耦:改动不即时污染 current,取消即丢弃)。
const fId = ref("");
const fName = ref("");
const fDesc = ref("");
const fBody = ref("");
const triggers = ref<string[]>([]);
const chipInput = ref("");
const bodyMode = ref<"edit" | "preview">("edit");
const formError = ref("");
const confirmDel = ref(false);
const deleting = ref(false);

// 依 current / editingNew 灌入表单:新建→空白;编辑→等 current 载入后填充。
watch(
  [current, editingNew],
  () => {
    formError.value = "";
    bodyMode.value = "edit";
    confirmDel.value = false;
    if (editingNew.value) {
      fId.value = "";
      fName.value = "";
      fDesc.value = "";
      fBody.value = "";
      triggers.value = [];
      return;
    }
    const c = current.value;
    if (c) {
      fId.value = c.id;
      fName.value = c.name;
      fDesc.value = c.description;
      fBody.value = c.body;
      triggers.value = [...c.triggers];
    }
  },
  { immediate: true },
);

function addTrigger(): void {
  const v = chipInput.value.trim();
  if (v && !triggers.value.includes(v)) triggers.value.push(v);
  chipInput.value = "";
}

function onChipKeydown(e: KeyboardEvent): void {
  if (e.key === "Enter") {
    e.preventDefault();
    addTrigger();
  } else if (e.key === "Backspace" && !chipInput.value && triggers.value.length) {
    triggers.value.pop();
  }
}

function removeTrigger(i: number): void {
  triggers.value.splice(i, 1);
}

function insertTemplate(): void {
  const cur = fBody.value.trim();
  fBody.value = (cur ? cur + "\n\n" : "") + TEMPLATE;
}

async function onSave(): Promise<void> {
  formError.value = "";
  const id = (editingNew.value ? fId.value : current.value?.id ?? "").trim();
  const name = fName.value.trim();
  // 未提交的触发词也一并纳入,免得用户忘了回车。
  addTrigger();
  if (editingNew.value && !ID_RE.test(id)) {
    formError.value = "标识 ID 需以字母或数字开头,只含字母、数字、下划线或连字符。";
    return;
  }
  if (!name) {
    formError.value = "名称不能为空。";
    return;
  }
  const payload: SopWrite = {
    id,
    name,
    description: fDesc.value.trim(),
    triggers: triggers.value,
    body: fBody.value,
  };
  await save(payload); // 成功→单例切到详情;失败→saveError 就地提示
}

function onCancel(): void {
  // 编辑既有篇→回其详情;新建→回列表。
  if (!editingNew.value && current.value) openDetail(current.value.id);
  else showList();
}

async function onDelete(): Promise<void> {
  const id = current.value?.id;
  if (!id) return;
  deleting.value = true;
  formError.value = "";
  try {
    await remove(id);
    showList();
  } catch (e) {
    formError.value = e instanceof Error ? e.message : String(e);
  } finally {
    deleting.value = false;
    confirmDel.value = false;
  }
}
</script>

<template>
  <main class="sv">
    <div class="sv__wrap">
      <button class="ed__back" type="button" @click="onCancel">
        <span aria-hidden="true">‹</span> {{ editingNew ? "返回列表" : "返回详情" }}
      </button>

      <!-- 编辑既有篇时正文仍在拉取 -->
      <section v-if="!editingNew && detailLoading" class="sv__state sv__state--soft">
        <p class="sv__stateBody">载入中…</p>
      </section>

      <section v-else-if="!editingNew && detailError" class="sv__state">
        <p class="sv__stateHd">打开失败</p>
        <p class="sv__stateBody">{{ detailError }}</p>
        <button class="sv__retry" type="button" @click="showList">返回列表</button>
      </section>

      <template v-else>
        <div class="ed__panel">
          <!-- 基本信息 -->
          <div class="ed__section">
            <p class="ed__legend">{{ editingNew ? "新建 SOP" : "编辑 SOP" }}</p>

            <div class="field">
              <label class="field__label" for="fId">标识 ID<span class="field__req">*</span></label>
              <input
                id="fId"
                v-model="fId"
                class="in in--mono"
                :disabled="!editingNew"
                placeholder="例如 travel-reimbursement"
              />
              <p class="hint">
                字母 / 数字开头,可含字母、数字、下划线与连字符;作为 OSS 内的文件名(<code>sops/&lt;id&gt;.md</code>),创建后不可修改。
              </p>
            </div>
            <div class="field">
              <label class="field__label" for="fName">名称<span class="field__req">*</span></label>
              <input id="fName" v-model="fName" class="in" placeholder="例如 差旅费报销" />
            </div>
            <div class="field">
              <label class="field__label" for="fDesc">简介</label>
              <input id="fDesc" v-model="fDesc" class="in" placeholder="一句话说明这篇流程解决什么问题" />
            </div>
            <div class="field">
              <label class="field__label">触发词</label>
              <div class="chips">
                <span v-for="(t, i) in triggers" :key="t" class="chip">
                  {{ t }}
                  <button class="chip__x" type="button" aria-label="删除触发词" @click="removeTrigger(i)">
                    ✕
                  </button>
                </span>
                <input
                  v-model="chipInput"
                  class="chips__in"
                  placeholder="输入后回车添加"
                  @keydown="onChipKeydown"
                  @blur="addTrigger"
                />
              </div>
              <p class="hint">用户提问命中这些关键词时,更容易匹配到本流程。回车添加,点 ✕ 删除。</p>
            </div>
          </div>

          <!-- 正文 -->
          <div class="ed__section">
            <div class="ed__legendrow">
              <p class="ed__legend">正文(Markdown)</p>
              <div class="ed__tools">
                <button class="link-btn" type="button" @click="insertTemplate">插入标准章节</button>
                <div class="seg" role="tablist" aria-label="正文视图">
                  <button
                    class="seg__btn"
                    type="button"
                    :class="{ 'is-active': bodyMode === 'edit' }"
                    @click="bodyMode = 'edit'"
                  >
                    编辑
                  </button>
                  <button
                    class="seg__btn"
                    type="button"
                    :class="{ 'is-active': bodyMode === 'preview' }"
                    @click="bodyMode = 'preview'"
                  >
                    预览
                  </button>
                </div>
              </div>
            </div>
            <textarea
              v-show="bodyMode === 'edit'"
              v-model="fBody"
              class="ta"
              placeholder="用 Markdown 书写流程正文……"
            />
            <MarkdownView v-if="bodyMode === 'preview'" class="preview" :source="fBody" />
          </div>
        </div>

        <p v-if="formError || saveError" class="ed__error" role="alert">
          {{ formError || saveError }}
        </p>

        <div class="ed__foot">
          <template v-if="!editingNew">
            <button
              v-if="!confirmDel"
              class="btn-del-strong"
              type="button"
              :disabled="deleting"
              @click="confirmDel = true"
            >
              删除此 SOP
            </button>
            <span v-else class="sop__confirm">
              <span class="sop__ask">确认删除?此操作不可撤销。</span>
              <button class="btn-del-strong" type="button" :disabled="deleting" @click="onDelete">
                {{ deleting ? "删除中…" : "确认删除" }}
              </button>
              <button class="btn-ghost" type="button" :disabled="deleting" @click="confirmDel = false">
                取消
              </button>
            </span>
          </template>
          <span v-else />

          <div class="ed__save">
            <button class="btn-ghost" type="button" :disabled="saving" @click="onCancel">取消</button>
            <button class="btn-primary" type="button" :disabled="saving" @click="onSave">
              {{ saving ? "保存中…" : "保存" }}
            </button>
          </div>
        </div>
      </template>
    </div>
  </main>
</template>
