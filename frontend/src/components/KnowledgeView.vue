<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import { useKnowledge } from "../composables/useKnowledge";
import { errorText, formatWhen } from "../lib/format";
import { isAdmin } from "../stores/auth";
import KnowledgeBreadcrumb from "./KnowledgeBreadcrumb.vue";
import KnowledgeToolbar from "./KnowledgeToolbar.vue";
import ReindexBar from "./ReindexBar.vue";

const {
  prefix,
  tree,
  loading,
  error,
  storageEnabled,
  indexReady,
  loadTree,
  enter,
  goto,
  refresh,
  isIndexed,
  removeFile,
  removeFolder,
  resumeJob,
  refreshManifest,
} = useKnowledge();

// 知识库对普通用户只读:上传 / 新建分类 / 删除 / 索引任务 / 版本回退只对管理员渲染
// (接口本身也都带管理员守卫,这里只是不把按钮摆出来)。
const canWrite = computed(() => isAdmin());

// 进入知识库视图即载入当前节点(单例保留了上次位置);
// 管理员再恢复索引任务进度(可能由别的标签页 / 上次访问发起)与版本信息 ——
// 这两个接口在 /admin/knowledge 前缀下,普通用户不请求。
onMounted(() => {
  void loadTree(prefix.value);
  if (canWrite.value) {
    void resumeJob();
    void refreshManifest();
  }
});

const folders = computed(() => tree.value?.folders ?? []);
const files = computed(() => tree.value?.files ?? []);
const isEmpty = computed(() => !folders.value.length && !files.value.length);

// 行内删除确认:同一时刻只有一行处于确认态(键 "d:名" 分类 / "f:key" 文件)。
const confirmKey = ref<string | null>(null);
// 正在删除的那行(禁用按钮防重复点)。
const deleting = ref<string | null>(null);
// 删除失败的就地提示(不把整个面板打成错误态)。
const opError = ref("");

async function delFile(key: string): Promise<void> {
  confirmKey.value = null;
  deleting.value = "f:" + key;
  opError.value = "";
  try {
    await removeFile(key);
  } catch (e) {
    opError.value = errorText(e);
  } finally {
    deleting.value = null;
  }
}

async function delFolder(name: string): Promise<void> {
  confirmKey.value = null;
  deleting.value = "d:" + name;
  opError.value = "";
  try {
    await removeFolder(prefix.value + name + "/");
  } catch (e) {
    opError.value = errorText(e);
  } finally {
    deleting.value = null;
  }
}

function formatSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

</script>

<template>
  <main class="kv">
    <div class="kv__wrap">
      <header class="pghead">
        <div>
          <h1 class="pghead__title">知识库</h1>
          <p class="pghead__sub">
            <template v-if="canWrite">管理各分类下的原件;上传 / 删除自动维护索引,版本回退在页面底部。</template>
            <template v-else>浏览各分类下的原件。</template>
          </p>
        </div>
        <div class="kv__aside">
          <span v-if="loading" class="kv__loading" aria-live="polite">载入中…</span>
          <button class="kv__refresh" type="button" :disabled="loading" @click="refresh">刷新</button>
        </div>
      </header>

      <!-- 只在进入子分类后需要面包屑;根节点与页标题同名,不再重复一行 -->
      <div v-if="prefix" class="kv__crumb">
        <KnowledgeBreadcrumb :prefix="prefix" @navigate="goto" />
      </div>

      <!-- 存储未接通:走空态,给出配置方向,不当报错 -->
      <section v-if="!storageEnabled" class="kv__state">
        <p class="kv__stateHd">知识库存储未接通</p>
        <p class="kv__stateBody">
          在 <code>backend/.env</code> 配置阿里云 OSS(私有桶 + RAM 最小权限)后重试。
        </p>
        <button class="kv__retry" type="button" @click="refresh">重试</button>
      </section>

      <!-- 载入失败 -->
      <section v-else-if="error" class="kv__state">
        <p class="kv__stateHd">载入失败</p>
        <p class="kv__stateBody">{{ error }}</p>
        <button class="kv__retry" type="button" @click="refresh">重试</button>
      </section>

      <!-- 已接通:工具条 + 内容 + 索引任务与版本(后两者仅管理员可见) -->
      <template v-else>
        <KnowledgeToolbar v-if="canWrite" />

        <p v-if="opError" class="kv__opError" role="alert">{{ opError }}</p>

        <section v-if="isEmpty && !loading" class="kv__state kv__state--soft">
          <p class="kv__stateHd">这个分类还是空的</p>
          <p class="kv__stateBody">
            <template v-if="canWrite">用上方「新建分类」或「上传文件」往这里添内容。</template>
            <template v-else>该分类下暂时没有文件。</template>
          </p>
        </section>

        <section v-else-if="!isEmpty" class="kv__panel">
          <p class="kv__count">{{ folders.length }} 个子分类 · {{ files.length }} 个文件</p>

          <ul v-if="folders.length" class="kv__list">
            <li v-for="name in folders" :key="'d:' + name" class="kv__row">
              <button
                class="kv__folder"
                type="button"
                :disabled="deleting === 'd:' + name"
                @click="enter(name)"
              >
                <span class="kv__glyph" aria-hidden="true">📁</span>
                <span class="kv__name">{{ name }}</span>
                <span class="kv__chev" aria-hidden="true">›</span>
              </button>
              <div v-if="canWrite" class="kv__act">
                <span v-if="deleting === 'd:' + name" class="kv__deleting">删除中…</span>
                <template v-else-if="confirmKey === 'd:' + name">
                  <span class="kv__ask">删整个分类?</span>
                  <button class="kv__yes" type="button" @click="delFolder(name)">删除</button>
                  <button class="kv__no" type="button" @click="confirmKey = null">取消</button>
                </template>
                <button
                  v-else
                  class="kv__del"
                  type="button"
                  aria-label="删除该分类及其下全部文件"
                  @click="confirmKey = 'd:' + name"
                >
                  ✕
                </button>
              </div>
            </li>
          </ul>

          <ul v-if="files.length" class="kv__list">
            <li v-for="f in files" :key="'f:' + f.key" class="kv__row kv__row--file">
              <span class="kv__glyph" aria-hidden="true">📄</span>
              <span class="kv__name" :title="f.name">{{ f.name }}</span>
              <span
                v-if="indexReady"
                class="kv__badge"
                :class="isIndexed(f.key) ? 'is-on' : 'is-off'"
              >
                {{ isIndexed(f.key) ? "已索引" : "未索引" }}
              </span>
              <span class="kv__size">{{ formatSize(f.size) }}</span>
              <span v-if="formatWhen(f.last_modified)" class="kv__when">{{ formatWhen(f.last_modified) }}</span>
              <div v-if="canWrite" class="kv__act">
                <span v-if="deleting === 'f:' + f.key" class="kv__deleting">删除中…</span>
                <template v-else-if="confirmKey === 'f:' + f.key">
                  <button class="kv__yes" type="button" @click="delFile(f.key)">删除</button>
                  <button class="kv__no" type="button" @click="confirmKey = null">取消</button>
                </template>
                <button
                  v-else
                  class="kv__del"
                  type="button"
                  aria-label="删除该文件"
                  @click="confirmKey = 'f:' + f.key"
                >
                  ✕
                </button>
              </div>
            </li>
          </ul>

          <p v-if="canWrite && files.length && !indexReady" class="kv__note">
            索引状态暂不可用(向量库未接通),不影响浏览。
          </p>
        </section>

        <ReindexBar v-if="canWrite" />
      </template>
    </div>
  </main>
</template>

<style scoped>
/* 页面标题区走全局 .pghead(style.css)。注意 .kv 这个类名是全局可见的:
   别的样式文件里不许出现裸 `.kv { ... }` 规则(metering.css 曾误留一条,
   把这里变成 flex 行容器,正文栏收缩、比别的标签页窄一截)。 */
/* 留白(上下左右)与调用与费用 / 账号页 / SOP 同一套口径:
   32px clamp(16px, 3vw, 48px) 48px,窄屏一档 20px 12px 32px
   —— 出处是 metering.css 的 .mt(:941 与 :995);auth.css 的 .pane、sop.css 的 .sv 同款。
   max-width 相同还不够:窗口没宽到顶住上限时,正文实际多宽由留白决定,四处必须一起改。 */
.kv {
  flex: 1;
  overflow-y: auto;
  padding: 32px clamp(16px, 3vw, 48px) 48px;
}
.kv__wrap {
  max-width: var(--maxw);
  margin: 0 auto;
}
@media (max-width: 540px) {
  .kv {
    padding: 20px 12px 32px;
  }
}
.kv__crumb {
  margin-bottom: 14px;
}
.kv__aside {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.kv__loading {
  color: var(--muted);
  font-size: 12.5px;
}
.kv__refresh {
  padding: 5px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.kv__refresh:hover:not(:disabled) {
  border-color: var(--primary);
  color: var(--primary);
}
.kv__refresh:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 状态卡片(存储未接通 / 错误 / 空节点) */
.kv__state {
  padding: 34px 24px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
  text-align: center;
}
.kv__state--soft {
  padding: 28px 24px;
  background: var(--surface-2);
  border-style: dashed;
}
.kv__stateHd {
  margin: 0 0 8px;
  color: var(--ink);
  font-size: 15px;
  font-weight: 600;
}
.kv__stateBody {
  margin: 0;
  color: var(--muted);
  font-size: 13.5px;
  line-height: 1.6;
}
.kv__stateBody code {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 12.5px;
  padding: 1px 5px;
  border-radius: var(--radius-xs);
  background: var(--surface-2);
}
.kv__retry {
  margin-top: 16px;
  padding: 7px 18px;
  border: 1px solid var(--primary);
  border-radius: var(--radius-sm);
  background: var(--primary-tint);
  color: var(--primary-strong);
  font: inherit;
  font-size: 13.5px;
  cursor: pointer;
}
.kv__retry:hover {
  border-color: var(--primary-strong);
}

.kv__opError {
  margin: 0 0 12px;
  padding: 9px 13px;
  border: 1px solid color-mix(in srgb, var(--seal) 32%, transparent);
  border-radius: var(--radius-sm);
  background: var(--seal-tint);
  color: var(--seal);
  font-size: 13px;
}

/* 内容面板 */
.kv__panel {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
  overflow: hidden;
}
.kv__count {
  margin: 0;
  padding: 11px 16px;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 12.5px;
}
.kv__list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.kv__list + .kv__list {
  border-top: 1px solid var(--line);
}
.kv__row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 14px;
  border-bottom: 1px solid var(--line);
}
.kv__row:last-child {
  border-bottom: 0;
}
.kv__row--file {
  padding: 10px 14px;
}
.kv__row:hover {
  background: var(--surface-2);
}
.kv__folder {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px 0;
  border: 0;
  background: transparent;
  font: inherit;
  color: var(--ink);
  text-align: left;
  cursor: pointer;
}
.kv__folder:disabled {
  cursor: default;
  opacity: 0.6;
}
.kv__glyph {
  font-size: 15px;
  line-height: 1;
  flex-shrink: 0;
}
.kv__name {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kv__folder .kv__name {
  font-weight: 500;
}
.kv__chev {
  color: var(--muted);
  font-size: 17px;
  line-height: 1;
  flex-shrink: 0;
}
.kv__badge {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11.5px;
  line-height: 1.5;
  white-space: nowrap;
}
.kv__badge.is-on {
  background: var(--primary-tint);
  color: var(--primary-strong);
}
.kv__badge.is-off {
  border: 1px solid var(--line);
  color: var(--muted);
}
.kv__size {
  flex-shrink: 0;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11.5px;
  color: var(--muted);
  min-width: 62px;
  text-align: right;
}
.kv__when {
  flex-shrink: 0;
  font-size: 11.5px;
  color: var(--muted);
  min-width: 82px;
  text-align: right;
}

/* 行内删除确认 */
.kv__act {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  min-height: 24px;
}
.kv__del {
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: var(--radius-xs);
  background: transparent;
  color: var(--muted);
  font-size: 13px;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.12s, color 0.12s, background 0.12s;
}
.kv__row:hover .kv__del,
.kv__del:focus-visible {
  opacity: 1;
}
.kv__del:hover {
  color: var(--seal);
  background: var(--seal-tint);
}
.kv__ask {
  color: var(--muted);
  font-size: 12px;
}
.kv__deleting {
  color: var(--muted);
  font-size: 12px;
}
.kv__yes,
.kv__no {
  height: 24px;
  padding: 0 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-xs);
  background: var(--surface);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.kv__yes {
  border-color: color-mix(in srgb, var(--seal) 40%, transparent);
  color: var(--seal);
}
.kv__yes:hover {
  background: var(--seal-tint);
}
.kv__no:hover {
  border-color: var(--primary);
  color: var(--primary);
}
.kv__note {
  margin: 0;
  padding: 10px 16px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 12px;
}

@media (max-width: 560px) {
  .kv__when {
    display: none;
  }
  .kv__del {
    opacity: 1;
  }
}
</style>
