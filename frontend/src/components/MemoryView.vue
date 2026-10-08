<script setup lang="ts">
// 我的记忆(§12):查看 / 检索 / 手添 / 编辑 / 删一条 / 彻底清除 / 补索引 / 功能状态。
//
// 界面口径(与后端 /api/v1/memory、docs/长期记忆系统技术方案.md 一致,改文案前先改文档):
// - 这里的每一行都是**你自己的**记忆:归属由后端按登录身份判定,页面里没有任何「指定用户」的入口;
// - 「待补索引」不是「没保存」:向量是派生数据,后台会补,本页给一键补索引;
// - 「暂停写入 / 暂停检索 / 暂停维护」都只是暂停某一路行为,**不删数据** —— 文案必须说清;
// - 删除分两种:删一条(这条不再被使用,聊天记录不受影响)与彻底清除(全清,但仍不删账号与对话)。
import { onActivated, onMounted, ref } from "vue";

import "../styles/memory.css";
import { useMemory } from "../composables/useMemory";
import { formatStamp, formatStampFull } from "../lib/format";
import type { MemoryItem } from "../api";

const {
  items, total, query, draft, status, statusError, loading, error, degraded, loaded,
  adding, addText, addError, editingId, editDraft, saving, editError, removingId, busyId,
  deleteNote, clearing, clearError, clearResult, reindexing, reindexNote,
  hasPrev, hasNext, firstIndex, lastIndex, searching, paused,
  refresh, search, clearSearch, goPage, add, startEdit, cancelEdit, saveEdit,
  askRemove, cancelRemove, remove, dismissDeleteNote, clearAll, dismissClearResult, reindex,
} = useMemory();

const clearDialog = ref<HTMLDialogElement | null>(null);
const understood = ref(false);

onMounted(() => void refresh());
// KeepAlive:从别的视图回来时补一次 —— 期间对话可能又提取出了新记忆。
onActivated(() => {
  if (loaded.value) void refresh({ keepPage: true });
});

function originText(origin: string): string {
  return origin === "user" ? "我手动添加" : "从对话里提取";
}

function openClear(): void {
  understood.value = false;
  clearDialog.value?.showModal();
}

function closeClear(): void {
  clearDialog.value?.close();
}

async function doClear(): Promise<void> {
  if (!understood.value) return;
  if (await clearAll()) {
    understood.value = false;
    closeClear();
  }
}

function threadHint(item: MemoryItem): string {
  return item.thread_id ? `来源对话:${item.thread_id}` : "手动添加,没有来源对话";
}
</script>

<template>
  <main class="mem">
    <div class="mem__wrap">
      <header class="pghead">
        <div>
          <h1 class="pghead__title">我的记忆</h1>
          <p class="pghead__sub">
            回答时会被记住的事实都在这儿，之后提问会作为参考带上一份。你可以随时改写、删掉它们。
            提取在回答结束后由后台完成（最终一致）：刚聊过的内容稍等片刻才会出现。
          </p>
        </div>
        <div class="mem__acts">
          <button
            class="mem__btn"
            type="button"
            :disabled="reindexing || !status || status.index_pending === 0"
            :title="status && status.index_pending === 0 ? '没有待补索引的记忆' : '把待补索引的记忆补写进向量库'"
            @click="reindex"
          >
            {{ reindexing ? "补索引中…" : `补索引${status && status.index_pending ? `(${status.index_pending})` : ""}` }}
          </button>
          <button class="mem__btn" type="button" :disabled="loading" @click="refresh({ keepPage: true })">
            {{ loading ? "刷新中…" : "刷新" }}
          </button>
        </div>
      </header>

      <!-- 功能状态:开关 + 规模 + 后台是否在跑(§12:必须显示开关状态) -->
      <section class="mstat" aria-label="记忆功能状态">
        <div class="mstat__cell">
          <span class="mstat__k">已记住</span>
          <span class="mstat__v">{{ status ? status.items : "—" }}<small>条</small></span>
        </div>
        <div class="mstat__cell">
          <span class="mstat__k">待补索引</span>
          <span class="mstat__v" :class="{ 'is-warn': (status?.index_pending ?? 0) > 0 }">
            {{ status ? status.index_pending : "—" }}<small>条</small>
          </span>
        </div>
        <div class="mstat__cell">
          <span class="mstat__k">后台任务</span>
          <span class="mstat__v">
            <template v-if="!status">—</template>
            <template v-else>待 {{ status.jobs.pending }} · 跑 {{ status.jobs.running }}</template>
          </span>
          <span class="mstat__x">{{ status?.worker_running ? "提取线程在跑" : "提取线程未启动" }}</span>
        </div>
        <!-- 清理台账:只在确有残留时出现(常态是 0/0,摆一排 0 只会占地方)。
             pending=后台还会重试;failed=重试用尽 —— 但两者都**只影响索引残留**:事实层早已删掉。 -->
        <div
          v-if="status && (status.cleanup_pending > 0 || status.cleanup_failed > 0)"
          class="mstat__cell"
        >
          <span class="mstat__k">索引清理</span>
          <span class="mstat__v" :class="{ 'is-warn': status.cleanup_failed > 0 }">
            {{ status.cleanup_pending }}<small>条待重试</small>
          </span>
          <span class="mstat__x">
            <template v-if="status.cleanup_failed > 0">
              {{ status.cleanup_failed }} 条重试用尽:记忆已删,但向量有残留
            </template>
            <template v-else>已删除的记忆,向量残留正在后台清理</template>
          </span>
        </div>
        <div class="mstat__cell">
          <span class="mstat__k">开关</span>
          <span class="mstat__v">
            <span v-if="!status">—</span>
            <span v-else-if="paused.length === 0" class="badge badge--ok">全部开启</span>
            <span v-else class="badge badge--warn">{{ paused.length }} 项暂停</span>
          </span>
          <span class="mstat__x">
            {{ status?.last_run_at ? `最近一轮 ${formatStamp(status.last_run_at)}` : "后台尚未跑过" }}
          </span>
        </div>
      </section>

      <p v-if="statusError" class="mem__note mem__note--warn" role="alert">
        功能状态读取失败：{{ statusError }}
      </p>
      <p v-for="p in paused" :key="p" class="mem__note">
        <strong>{{ p }}</strong> —— 只是暂停，不是删除：已记住的下面照常可见、可改、可删；开关由部署配置控制。
      </p>
      <p v-if="status && !status.configured" class="mem__note mem__note--warn">
        长期记忆未配置（后端没连数据库）：本页读不到内容，聊天照常可用。
      </p>
      <p v-if="status && !status.enabled" class="mem__note mem__note--warn">
        长期记忆已关闭：本次不读也不写记忆，已存数据仍在库里，打开开关即可恢复。
      </p>
      <p v-if="status?.last_error" class="mem__note mem__note--warn">
        后台任务最近一次失败：{{ status.last_error }}（会按退避重试；也可以稍后手动补一次索引）
      </p>
      <p v-if="reindexNote" class="mem__note" role="status">{{ reindexNote }}</p>
      <!-- 删除的收尾是两步:事实立刻删掉,向量清理可能要后台重试 —— 照实说,不假装清完了 -->
      <p v-if="deleteNote" class="mem__note" role="status">
        {{ deleteNote }}
        <button class="mem__btn mem__btn--link" type="button" @click="dismissDeleteNote">知道了</button>
      </p>

      <!-- 检索 + 手添 -->
      <div class="mem__tools">
        <form class="mem__field mem__field--search" @submit.prevent="search">
          <input
            v-model="draft"
            class="mem__input"
            type="search"
            maxlength="200"
            placeholder="检索我的记忆，例如「报销」「发票」"
            aria-label="检索我的记忆"
          />
          <button class="mem__btn mem__btn--primary" type="submit" :disabled="loading">检索</button>
          <button v-if="searching" class="mem__btn" type="button" @click="clearSearch">回到列表</button>
        </form>

        <form class="mem__field" @submit.prevent="add">
          <input
            v-model="addText"
            class="mem__input"
            type="text"
            maxlength="4000"
            placeholder="手动记一条，例如「我习惯把发票按项目分摊」"
            aria-label="手动添加一条记忆"
          />
          <button
            class="mem__btn mem__btn--primary"
            type="submit"
            :disabled="!addText.trim() || adding"
          >
            {{ adding ? "保存中…" : "记住" }}
          </button>
        </form>
      </div>
      <p v-if="addError" class="mem__err" role="alert">{{ addError }}</p>

      <!-- 列表 -->
      <div v-if="loading && !items.length" class="mem__state">正在加载…</div>
      <div v-else-if="error" class="mem__state mem__state--err" role="alert">
        <p>{{ error }}</p>
        <button class="mem__btn" type="button" @click="refresh()">重试</button>
      </div>
      <div v-else-if="!items.length" class="mem__state">
        <template v-if="searching">
          没有检索到和「{{ query }}」相关的记忆。换个说法试试，或回到列表看看全部。
        </template>
        <template v-else>
          还没有记忆。对话里提到的事实会被自动记下来（回答完整结束后由后台提取），也可以在上面手动记一条。
        </template>
      </div>

      <template v-else>
        <p v-if="searching" class="mem__sub">
          按相关性找到 {{ items.length }} 条（检索不做分页，只给最相关的若干条）。
        </p>
        <p v-if="degraded.length" class="mem__note">
          本次检索有降级：{{ degraded.join("；") }}
        </p>

        <ul class="mem__list">
          <li v-for="item in items" :key="item.id" class="mcard">
            <div class="mcard__main">
              <template v-if="editingId === item.id">
                <textarea
                  v-model="editDraft"
                  class="mcard__edit"
                  rows="3"
                  maxlength="4000"
                  :aria-label="`改写记忆：${item.text}`"
                />
                <p v-if="editError" class="mem__err" role="alert">{{ editError }}</p>
                <div class="mcard__acts">
                  <button
                    class="mem__btn mem__btn--primary"
                    type="button"
                    :disabled="!editDraft.trim() || saving"
                    @click="saveEdit"
                  >
                    {{ saving ? "保存中…" : "保存" }}
                  </button>
                  <button class="mem__btn" type="button" :disabled="saving" @click="cancelEdit">取消</button>
                </div>
              </template>

              <template v-else>
                <p class="mcard__text">{{ item.text }}</p>
                <div class="mcard__meta">
                  <span class="badge">{{ originText(item.origin) }}</span>
                  <span class="badge" :title="`入库 ${formatStampFull(item.created_at)}`">
                    更新 {{ formatStamp(item.updated_at) }}
                  </span>
                  <span class="badge" :title="threadHint(item)">
                    <template v-if="item.thread_id">来自某次对话</template>
                    <template v-else>手动添加</template>
                  </span>
                  <span
                    v-if="item.index_state === 'synced'"
                    class="badge badge--ok"
                    :title="item.indexed_at ? `索引于 ${formatStampFull(item.indexed_at)}` : '向量已同步'"
                  >
                    索引已同步
                  </span>
                  <span v-else class="badge badge--warn" title="向量是派生数据：记忆已经保存，后台会补上">
                    待补索引
                  </span>
                  <span v-if="item.revision > 1" class="badge">改过 {{ item.revision - 1 }} 次</span>
                </div>

                <div class="mcard__acts">
                  <button
                    class="mem__btn mem__btn--link"
                    type="button"
                    :disabled="busyId === item.id"
                    @click="startEdit(item)"
                  >
                    编辑
                  </button>
                  <button
                    v-if="removingId !== item.id"
                    class="mem__btn mem__btn--link mem__btn--danger"
                    type="button"
                    :disabled="busyId === item.id"
                    @click="askRemove(item.id)"
                  >
                    删除
                  </button>
                  <template v-else>
                    <span class="mcard__warn">
                      删除后这条不再被使用，向量也会清掉；那次对话本身不受影响。
                    </span>
                    <button
                      class="mem__btn mem__btn--danger"
                      type="button"
                      :disabled="busyId === item.id"
                      @click="remove(item)"
                    >
                      {{ busyId === item.id ? "删除中…" : "确认删除" }}
                    </button>
                    <button class="mem__btn" type="button" @click="cancelRemove">取消</button>
                  </template>
                </div>
              </template>
            </div>
          </li>
        </ul>

        <nav v-if="!searching && total > items.length" class="mem__pager" aria-label="分页">
          <button class="mem__btn" type="button" :disabled="!hasPrev || loading" @click="goPage(-1)">上一页</button>
          <span class="mem__pagerTxt">第 {{ firstIndex }}–{{ lastIndex }} 条，共 {{ total }} 条</span>
          <button class="mem__btn" type="button" :disabled="!hasNext || loading" @click="goPage(1)">下一页</button>
        </nav>
      </template>

      <!-- 彻底清除:与「删一条」分开写,确认里逐条说清会删什么、不会删什么 -->
      <section class="mem__danger">
        <div>
          <h2 class="mem__dangerT">彻底删除我的长期记忆</h2>
          <p class="mem__dangerB">
            删除全部已记住的事实、待执行的提取任务与它们的向量索引。聊天记录与账号不受影响，
            清除之后新交流还会重新提取出同样的事实。
          </p>
        </div>
        <button class="mem__btn mem__btn--danger" type="button" @click="openClear">彻底删除…</button>
      </section>

      <p v-if="clearResult" class="mem__note" role="status">
        已清除 {{ clearResult.items }} 条记忆、{{ clearResult.jobs }} 个待执行任务，脱敏 {{ clearResult.history }} 条审计正文；
        记忆代次已推进到 {{ clearResult.generation }}（旧任务据此作废）。
        <template v-if="clearResult.cleanup === 'pending'">
          向量索引还没清完（已登记清理台账，后台按退避继续重试；事实与待执行任务都已删除）。
        </template>
        <template v-else-if="!clearResult.vectors">
          索引有残留（事实已删，可稍后重建）。
        </template>
        <template v-else>索引已清干净（本次清掉 {{ clearResult.points }} 个向量点）。</template>
        <template v-if="clearResult.degraded.length">未做到最好的地方：{{ clearResult.degraded.join("；") }}</template>
        <button class="mem__btn mem__btn--link" type="button" @click="dismissClearResult">知道了</button>
      </p>
    </div>

    <dialog ref="clearDialog" class="mem__dialog" aria-labelledby="mem-clear-title" @close="understood = false">
      <div class="mem__dialogBody">
        <h3 id="mem-clear-title" class="mem__dialogT">彻底删除全部长期记忆？</h3>
        <ul class="mem__dialogList">
          <li>会删除：全部已记住的事实、待执行的提取任务（含任务里暂存的对话正文）、以及它们的向量索引。</li>
          <li>会保留：变更审计行，但正文会被脱敏（清掉）。</li>
          <li>不会动：你的账号与聊天记录 —— 对话还在，之后的交流还会重新提取出同样的事实。</li>
          <li>
            {{ status ? `当前有 ${status.items} 条记忆会被删除。` : "" }}删除后无法恢复。
          </li>
        </ul>
        <label class="mem__check">
          <input v-model="understood" type="checkbox" />
          <span>我已了解：这会删除我的全部长期记忆，且无法恢复。</span>
        </label>
        <p v-if="clearError" class="mem__err" role="alert">{{ clearError }}</p>
        <div class="mem__dialogActs">
          <button class="mem__btn" type="button" @click="closeClear">取消</button>
          <button
            class="mem__btn mem__btn--danger"
            type="button"
            :disabled="!understood || clearing"
            @click="doClear"
          >
            {{ clearing ? "删除中…" : "彻底删除" }}
          </button>
        </div>
      </div>
    </dialog>
  </main>
</template>
