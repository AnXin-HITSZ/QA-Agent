<script setup lang="ts">
// 我的账号:资料 / 改密 / 登录设备。
// 两条会「把自己踢回登录页」的动作在这里要交代清楚:改密与「退出全部设备」都会撤销全部会话
// (含当前这次),后端返回的说明经 setFlash 带到登录页显示 —— 否则用户只会莫名其妙回到登录页。
import { computed, onMounted, ref } from "vue";

import { formatStamp, formatStampFull } from "../../lib/format";
import { navigate } from "../../lib/route";
import {
  AuthError,
  authState,
  changePassword,
  listSessions,
  logout,
  logoutAll,
  revokeSession,
  setFlash,
  type SessionInfo,
} from "../../stores/auth";

const STATUS_TEXT: Record<string, string> = {
  pending_email: "待验证邮箱",
  pending_approval: "待审批",
  active: "正常",
  rejected: "未通过审批",
  disabled: "已停用",
};

const me = computed(() => authState.user);

// ── 修改密码 ──
const current = ref("");
const next = ref("");
const confirm = ref("");
const pwBusy = ref(false);
const pwError = ref("");

const canSubmitPw = computed(() => !!current.value && !!next.value && !pwBusy.value);

async function submitPassword(): Promise<void> {
  if (!canSubmitPw.value) return;
  pwError.value = "";
  if (next.value !== confirm.value) {
    pwError.value = "两次输入的新密码不一致。";
    return;
  }
  if (next.value === current.value) {
    pwError.value = "新密码不能与当前密码相同。";
    return;
  }
  pwBusy.value = true;
  try {
    const message = await changePassword(current.value, next.value);
    // 到这里会话已被后端撤销、本地也已清空(App 会切到登录页);把原因写在登录页上。
    setFlash(message);
    navigate("login");
  } catch (e) {
    pwError.value = e instanceof AuthError ? e.message : e instanceof Error ? e.message : String(e);
  } finally {
    pwBusy.value = false;
    current.value = "";
    next.value = "";
    confirm.value = "";
  }
}

// ── 登录设备 ──
const sessions = ref<SessionInfo[]>([]);
const sessionsLoading = ref(true);
const sessionsError = ref("");
const notice = ref("");
const revoking = ref(""); // 正在下线的会话 id;"" = 没有在途请求

async function loadSessions(): Promise<void> {
  sessionsLoading.value = true;
  sessionsError.value = "";
  try {
    sessions.value = await listSessions();
  } catch (e) {
    sessionsError.value = e instanceof AuthError ? e.message : e instanceof Error ? e.message : String(e);
  } finally {
    sessionsLoading.value = false;
  }
}

async function drop(session: SessionInfo): Promise<void> {
  if (revoking.value) return;
  revoking.value = session.id;
  notice.value = "";
  try {
    await revokeSession(session.id);
    if (session.current) {
      // 下线的是自己这台:本地状态必须跟着清(后端已撤销,留着只会到处 401)。
      const message = "已在本设备退出登录。";
      setFlash(message);
      navigate("login");
      return;
    }
    sessions.value = sessions.value.filter((s) => s.id !== session.id);
    notice.value = "该设备已下线。";
  } catch (e) {
    sessionsError.value = e instanceof AuthError ? e.message : e instanceof Error ? e.message : String(e);
  } finally {
    revoking.value = "";
  }
}

// ── 退出当前 / 全部 ──
const busy = ref(false);

async function signOut(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    await logout();
  } catch {
    // 服务端失败也照样退:logout() 内部已经清了本地状态
  } finally {
    busy.value = false;
    navigate("login");
  }
}

async function signOutAll(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    await logoutAll();
  } catch (e) {
    sessionsError.value = e instanceof AuthError ? e.message : e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
    navigate("login");
  }
}

// 客户端标识:后端存的是原始 UA,列表里给一个人读的简称(原始串留在 title 里可查)。
function uaLabel(ua: string): string {
  if (!ua) return "未知客户端";
  const browser = /Edg\//i.test(ua) ? "Edge"
    : /OPR\//i.test(ua) ? "Opera"
    : /Firefox\//i.test(ua) ? "Firefox"
    : /Chrome\//i.test(ua) ? "Chrome"
    : /Safari\//i.test(ua) ? "Safari"
    : "";
  const os = /Windows/i.test(ua) ? "Windows"
    : /iPhone|iPad|iPod/i.test(ua) ? "iOS"
    : /Android/i.test(ua) ? "Android"
    : /Mac OS X/i.test(ua) ? "macOS"
    : /Linux/i.test(ua) ? "Linux"
    : "";
  const label = [browser, os].filter(Boolean).join(" · ");
  return label || "未知客户端";
}

onMounted(() => void loadSessions());
</script>

<template>
  <div class="pane">
    <div class="pane__inner">
      <div class="pane__head">
        <div>
          <h1 class="pane__title">我的账号</h1>
          <p class="pane__sub">资料、密码与登录设备。</p>
        </div>
      </div>

      <!-- ── 资料 ── -->
      <section class="card">
        <h2 class="card__title">基本资料</h2>
        <p class="card__sub">邮箱与显示名由管理员维护;角色与状态改动后需要重新登录才生效。</p>
        <dl v-if="me" class="kv">
          <dt>邮箱</dt>
          <dd>
            {{ me.email }}
            <span class="badge" :class="me.email_verified ? 'badge--ok' : 'badge--warn'">
              {{ me.email_verified ? "已验证" : "未验证" }}
            </span>
          </dd>
          <dt>显示名</dt>
          <dd>{{ me.display_name || "—" }}</dd>
          <dt>角色</dt>
          <dd>{{ me.role === "admin" ? "管理员" : "普通用户" }}</dd>
          <dt>状态</dt>
          <dd>{{ STATUS_TEXT[me.status] ?? me.status }}</dd>
          <dt>注册时间</dt>
          <dd>{{ formatStampFull(me.created_at) || "—" }}</dd>
          <dt>最近登录</dt>
          <dd>{{ formatStampFull(me.last_login_at) || "—" }}</dd>
        </dl>
      </section>

      <!-- ── 修改密码 ── -->
      <section class="card">
        <h2 class="card__title">修改密码</h2>
        <p class="card__sub">
          改完之后所有设备都会退出登录(包括当前这台),需要用新密码重新登录。
        </p>
        <form novalidate @submit.prevent="submitPassword">
          <div class="auth__field">
            <label class="auth__label" for="pw-current">当前密码</label>
            <input
              id="pw-current"
              v-model="current"
              class="auth__input"
              type="password"
              autocomplete="current-password"
              :disabled="pwBusy"
            />
          </div>
          <div class="auth__field">
            <label class="auth__label" for="pw-next">新密码</label>
            <input
              id="pw-next"
              v-model="next"
              class="auth__input"
              type="password"
              autocomplete="new-password"
              :disabled="pwBusy"
            />
            <p class="auth__hint">8–128 个字符,可用空格与中文;不强制大小写数字组合。</p>
          </div>
          <div class="auth__field">
            <label class="auth__label" for="pw-confirm">确认新密码</label>
            <input
              id="pw-confirm"
              v-model="confirm"
              class="auth__input"
              type="password"
              autocomplete="new-password"
              :disabled="pwBusy"
            />
          </div>
          <div class="auth__actions">
            <button class="mini mini--primary" type="submit" :disabled="!canSubmitPw">
              {{ pwBusy ? "提交中…" : "修改密码" }}
            </button>
          </div>
        </form>
        <p v-if="pwError" class="auth__note auth__note--err" role="alert">{{ pwError }}</p>
      </section>

      <!-- ── 登录设备 ── -->
      <section class="card">
        <h2 class="card__title">登录设备</h2>
        <p class="card__sub">当前账号还在哪些设备上登录着。不认识的设备请立刻下线并修改密码。</p>

        <p v-if="sessionsLoading" class="auth__spin" role="status">正在读取…</p>

        <div v-else-if="sessions.length" class="tbl__scroll">
          <table class="tbl">
            <thead>
              <tr>
                <th>设备</th>
                <th>IP</th>
                <th>登录时间</th>
                <th>最近活动</th>
                <th>到期</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="s in sessions" :key="s.id">
                <td>
                  <span :title="s.user_agent || undefined">{{ uaLabel(s.user_agent) }}</span>
                  <span v-if="s.current" class="badge badge--ok">当前设备</span>
                </td>
                <td class="tbl__mono">{{ s.ip || "—" }}</td>
                <td class="tbl__mono">{{ formatStamp(s.created_at) || "—" }}</td>
                <td class="tbl__mono">{{ formatStamp(s.last_used_at) || "—" }}</td>
                <td class="tbl__mono">{{ formatStamp(s.expires_at) || "—" }}</td>
                <td>
                  <button
                    class="mini"
                    :class="{ 'mini--danger': !s.current }"
                    type="button"
                    :disabled="!!revoking"
                    @click="drop(s)"
                  >
                    {{ revoking === s.id ? "下线中…" : s.current ? "退出登录" : "下线" }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <p v-else class="auth__hint">没有其它登录设备。</p>

        <p v-if="sessionsError" class="auth__note auth__note--err" role="alert">{{ sessionsError }}</p>
        <p v-if="notice" class="auth__note auth__note--ok" role="status">{{ notice }}</p>

        <div class="auth__actions">
          <button class="mini" type="button" :disabled="busy" @click="signOut">退出登录</button>
          <button class="mini mini--danger" type="button" :disabled="busy" @click="signOutAll">
            退出全部设备
          </button>
        </div>
      </section>
    </div>
  </div>
</template>
