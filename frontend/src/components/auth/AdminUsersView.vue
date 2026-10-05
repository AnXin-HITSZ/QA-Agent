<script setup lang="ts">
// 管理员:用户审批 / 启停 / 角色 / 审计。
// 界面上的按钮只是礼貌 —— 真正的授权在后端每条路由的 require_admin 上,状态流转是原子 CAS
// (两个管理员同时点同一个账号,抢输的一方会拿到 409,这里把 409 的话原样显示出来)。
// 对自己的行不给「停用 / 改角色」:后端也拒绝(避免唯一的 admin 自锁在门外)。
import { computed, onMounted, ref } from "vue";

import { formatStamp, formatStampFull } from "../../lib/format";
import {
  AuthError,
  authState,
  listAudit,
  listUsers,
  reviewUser,
  setUserRole,
  type AdminAction,
  type AdminUser,
  type AdminUserPage,
  type AuditEntry,
} from "../../stores/auth";

const LIMIT = 20;

const STATUS_FILTERS: Array<{ value: string; label: string }> = [
  { value: "", label: "全部" },
  { value: "pending_email", label: "待验证" },
  { value: "pending_approval", label: "待审批" },
  { value: "active", label: "正常" },
  { value: "rejected", label: "已拒绝" },
  { value: "disabled", label: "已停用" },
];

const STATUS_TEXT: Record<string, string> = {
  pending_email: "待验证邮箱",
  pending_approval: "待审批",
  active: "正常",
  rejected: "未通过审批",
  disabled: "已停用",
};

const ACTION_TEXT: Record<string, string> = {
  approve: "已通过审批",
  reject: "已拒绝",
  disable: "已停用",
  enable: "已启用",
};

const meId = computed(() => authState.user?.id ?? "");

// ── 列表 ──
const status = ref("");
const q = ref("");
const offset = ref(0);
const page = ref<AdminUserPage>({ items: [], total: 0, counts: {} });
const loading = ref(true);
const error = ref("");
const notice = ref("");

const total = computed(() => page.value.total);
const shownFrom = computed(() => (total.value === 0 ? 0 : offset.value + 1));
const shownTo = computed(() => Math.min(offset.value + LIMIT, total.value));
const hasPrev = computed(() => offset.value > 0);
const hasNext = computed(() => offset.value + LIMIT < total.value);

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    page.value = await listUsers({ status: status.value, q: q.value.trim(), offset: offset.value, limit: LIMIT });
  } catch (e) {
    error.value = e instanceof AuthError ? e.message : e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

function pickStatus(value: string): void {
  if (status.value === value) return;
  status.value = value;
  offset.value = 0; // 换筛选条件必须回第一页,否则可能停在一个空页上
  void load();
}

function search(): void {
  offset.value = 0;
  void load();
}

function go(delta: number): void {
  offset.value = Math.max(0, offset.value + delta * LIMIT);
  void load();
}

// ── 动作 ──
const busyId = ref("");
const confirming = ref(""); // `${action}:${id}` —— 破坏性动作要按两下
const rejectFor = ref(""); // 正在填拒绝理由的用户 id
const rejectNote = ref("");

function confirmKey(action: string, id: string): string {
  return `${action}:${id}`;
}

async function act(user: AdminUser, action: AdminAction, note = ""): Promise<void> {
  if (busyId.value) return;
  busyId.value = user.id;
  error.value = "";
  notice.value = "";
  try {
    await reviewUser(user.id, action, note);
    notice.value = `${ACTION_TEXT[action]}:${user.email}`;
    rejectFor.value = "";
    rejectNote.value = "";
    await load();
  } catch (e) {
    error.value = e instanceof AuthError ? e.message : e instanceof Error ? e.message : String(e);
  } finally {
    busyId.value = "";
    confirming.value = "";
  }
}

async function switchRole(user: AdminUser): Promise<void> {
  if (busyId.value) return;
  busyId.value = user.id;
  error.value = "";
  notice.value = "";
  try {
    const next = user.role === "admin" ? "user" : "admin";
    await setUserRole(user.id, next);
    notice.value = `${user.email} 的角色已改为 ${next === "admin" ? "管理员" : "普通用户"}`;
    await load();
  } catch (e) {
    error.value = e instanceof AuthError ? e.message : e instanceof Error ? e.message : String(e);
  } finally {
    busyId.value = "";
    confirming.value = "";
  }
}

// ── 审计 ──
const auditOpen = ref(false);
const auditFor = ref(""); // "" = 全局最近记录;否则只看某个用户
const audit = ref<AuditEntry[]>([]);
const auditLoading = ref(false);
const auditError = ref("");

async function loadAudit(): Promise<void> {
  auditLoading.value = true;
  auditError.value = "";
  try {
    audit.value = await listAudit(auditFor.value || undefined, 100);
  } catch (e) {
    auditError.value = e instanceof AuthError ? e.message : e instanceof Error ? e.message : String(e);
  } finally {
    auditLoading.value = false;
  }
}

function toggleAudit(userId = ""): void {
  const same = auditOpen.value && auditFor.value === userId;
  auditFor.value = userId;
  auditOpen.value = !same;
  if (auditOpen.value) void loadAudit();
}

onMounted(() => void load());
</script>

<template>
  <div class="pane">
    <div class="pane__inner">
      <div class="pane__head">
        <div>
          <h1 class="pane__title">用户管理</h1>
          <p class="pane__sub">审批注册、启停账号、调整角色。所有动作都记进审计。</p>
        </div>
        <button class="mini" type="button" @click="toggleAudit()">
          {{ auditOpen && !auditFor ? "收起审计" : "最近操作审计" }}
        </button>
      </div>

      <!-- ── 筛选 ── -->
      <section class="card">
        <div class="row">
          <div class="row" role="group" aria-label="按状态筛选">
            <button
              v-for="f in STATUS_FILTERS"
              :key="f.value"
              class="mini"
              type="button"
              :class="{ 'mini--primary': status === f.value }"
              :aria-pressed="status === f.value"
              @click="pickStatus(f.value)"
            >
              {{ f.label }}
              <span class="tbl__mono">{{ f.value ? (page.counts[f.value] ?? 0) : total }}</span>
            </button>
          </div>
          <form class="row grow" role="search" @submit.prevent="search">
            <label class="auth__label" for="user-q">搜索</label>
            <input
              id="user-q"
              v-model="q"
              class="auth__input grow"
              type="search"
              placeholder="邮箱或昵称"
              @keyup.enter="search"
            />
            <button class="mini" type="submit" :disabled="loading">搜索</button>
          </form>
        </div>
        <p class="card__sub card__sub--tail">
          状态人数是<strong>全量</strong>计数(不受搜索框影响);搜索只在当前筛选里挑。
        </p>
      </section>

      <p v-if="error" class="auth__note auth__note--err" role="alert">{{ error }}</p>
      <p v-if="notice" class="auth__note auth__note--ok" role="status">{{ notice }}</p>

      <!-- ── 列表 ── -->
      <section class="card">
        <p v-if="loading" class="auth__spin" role="status">正在读取…</p>

        <template v-else-if="page.items.length">
          <div class="tbl__scroll">
            <table class="tbl">
              <thead>
                <tr>
                  <th>账号</th>
                  <th>角色</th>
                  <th>状态</th>
                  <th>注册</th>
                  <th>最近登录</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <template v-for="u in page.items" :key="u.id">
                  <tr>
                    <td>
                      <div>{{ u.display_name || "—" }}</div>
                      <div class="tbl__mono">{{ u.email }}</div>
                      <span v-if="u.id === meId" class="badge">这是你自己</span>
                    </td>
                    <td>
                      <span class="badge" :class="u.role === 'admin' ? 'badge--ok' : ''">
                        {{ u.role === "admin" ? "管理员" : "普通用户" }}
                      </span>
                    </td>
                    <td>
                      <div>{{ STATUS_TEXT[u.status] ?? u.status }}</div>
                      <div v-if="!u.email_verified" class="tbl__mono">邮箱未验证</div>
                      <div v-if="u.review_note" class="tbl__mono" :title="u.review_note">
                        备注:{{ u.review_note }}
                      </div>
                    </td>
                    <td class="tbl__mono">{{ formatStamp(u.created_at) || "—" }}</td>
                    <td class="tbl__mono">{{ formatStamp(u.last_login_at) || "—" }}</td>
                    <td>
                      <div class="tbl__acts">
                        <!-- 待验证 / 待审批:审批 -->
                        <template v-if="u.status === 'pending_email' || u.status === 'pending_approval'">
                          <button
                            class="mini mini--primary"
                            type="button"
                            :disabled="!!busyId"
                            @click="act(u, 'approve')"
                          >
                            {{ busyId === u.id ? "处理中…" : "通过" }}
                          </button>
                          <button
                            class="mini mini--danger"
                            type="button"
                            :disabled="!!busyId"
                            @click="rejectFor = rejectFor === u.id ? '' : u.id"
                          >
                            拒绝
                          </button>
                        </template>

                        <!-- 已拒绝:可以改成通过 -->
                        <button
                          v-else-if="u.status === 'rejected'"
                          class="mini"
                          type="button"
                          :disabled="!!busyId"
                          @click="act(u, 'approve')"
                        >
                          改为通过
                        </button>

                        <!-- 正常:停用(不能停自己) -->
                        <template v-else-if="u.status === 'active'">
                          <button
                            v-if="u.id !== meId"
                            class="mini"
                            type="button"
                            :disabled="!!busyId"
                            @click="confirming = confirming === confirmKey('disable', u.id) ? '' : confirmKey('disable', u.id)"
                          >
                            停用
                          </button>
                          <button
                            v-if="u.id !== meId && confirming === confirmKey('disable', u.id)"
                            class="mini mini--danger"
                            type="button"
                            :disabled="!!busyId"
                            @click="act(u, 'disable')"
                          >
                            确认停用
                          </button>
                        </template>

                        <!-- 已停用:启用 -->
                        <button
                          v-else-if="u.status === 'disabled'"
                          class="mini"
                          type="button"
                          :disabled="!!busyId"
                          @click="act(u, 'enable')"
                        >
                          启用
                        </button>

                        <!-- 角色(只有 active 的账号能改;不能改自己) -->
                        <template v-if="u.status === 'active' && u.id !== meId">
                          <button
                            v-if="confirming !== confirmKey('role', u.id)"
                            class="mini"
                            type="button"
                            :disabled="!!busyId"
                            @click="confirming = confirmKey('role', u.id)"
                          >
                            {{ u.role === "admin" ? "降为普通用户" : "设为管理员" }}
                          </button>
                          <button
                            v-else
                            class="mini mini--danger"
                            type="button"
                            :disabled="!!busyId"
                            @click="switchRole(u)"
                          >
                            确认改角色
                          </button>
                        </template>

                        <button class="mini" type="button" @click="toggleAudit(u.id)">审计</button>
                      </div>
                    </td>
                  </tr>

                  <!-- 拒绝理由(可选但建议写):展开在行下方,不弹窗、不打乱表格 -->
                  <tr v-if="rejectFor === u.id">
                    <td colspan="6">
                      <div class="row">
                        <label class="auth__label" :for="`note-${u.id}`">拒绝理由(可选)</label>
                        <input
                          :id="`note-${u.id}`"
                          v-model="rejectNote"
                          class="auth__input grow"
                          type="text"
                          maxlength="255"
                          placeholder="例如:未提供实验室门禁姓名"
                        />
                        <button
                          class="mini mini--danger"
                          type="button"
                          :disabled="!!busyId"
                          @click="act(u, 'reject', rejectNote)"
                        >
                          确认拒绝
                        </button>
                        <button class="mini" type="button" @click="rejectFor = ''">取消</button>
                      </div>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>

          <div class="row row--sub">
            <span class="tbl__mono">第 {{ shownFrom }}–{{ shownTo }} 条,共 {{ total }} 条</span>
            <button class="mini" type="button" :disabled="!hasPrev || loading" @click="go(-1)">上一页</button>
            <button class="mini" type="button" :disabled="!hasNext || loading" @click="go(1)">下一页</button>
          </div>
        </template>

        <p v-else class="auth__hint">
          {{ q ? "没有匹配的账号。" : "这个筛选下还没有账号。" }}
        </p>
      </section>

      <!-- ── 审计 ── -->
      <section v-if="auditOpen" class="card">
        <h2 class="card__title">
          {{ auditFor ? "该账号的操作记录" : "最近操作记录" }}
        </h2>
        <p class="card__sub">
          谁在什么时候对谁做了什么。系统动作(注册、登录、刷新重放)的操作者为空。
        </p>

        <p v-if="auditLoading" class="auth__spin" role="status">正在读取…</p>
        <p v-else-if="auditError" class="auth__note auth__note--err" role="alert">{{ auditError }}</p>

        <div v-else-if="audit.length" class="tbl__scroll">
          <table class="tbl">
            <thead>
              <tr>
                <th>时间</th>
                <th>动作</th>
                <th>结果</th>
                <th>账号</th>
                <th>IP</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(a, i) in audit" :key="`${a.at}-${a.action}-${i}`">
                <td class="tbl__mono">{{ formatStampFull(a.at) }}</td>
                <td class="tbl__mono">{{ a.action }}</td>
                <td>
                  <span class="badge" :class="a.result === 'ok' ? 'badge--ok' : 'badge--warn'">
                    {{ a.result }}
                  </span>
                </td>
                <td class="tbl__mono">{{ a.email || a.target_user_id || "—" }}</td>
                <td class="tbl__mono">{{ a.ip || "—" }}</td>
                <td>{{ a.note || "—" }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <p v-else class="auth__hint">还没有记录。</p>
      </section>
    </div>
  </div>
</template>
