<script setup lang="ts">
// 登录。状态不对的账号(待验证 / 待审批 / 被拒 / 停用)后端只在**密码正确之后**才说,
// 前端据此把提示换成一屏说明,并给「重发验证邮件」这一条出路;密码错就是密码错,不泄露别的。
import { computed, ref } from "vue";

import { navigate } from "../../lib/route";
import { AuthError, authState, login, resendVerification } from "../../stores/auth";

defineProps<{ route: { token: string } }>();

const email = ref("");
const password = ref("");
const busy = ref(false);
const error = ref("");
const notice = ref("");
const statusCode = ref(""); // account_pending_email / account_pending_approval / …

// 后端状态码 → 给用户看的一句话(与后端 _status_message 同一口径,前端只是排一下版)。
const statusText = computed(() => {
  switch (statusCode.value) {
    case "account_pending_email":
      return "这个账号还没有验证邮箱。请点开注册时那封验证邮件里的链接;没收到可以重新发一封。";
    case "account_pending_approval":
      return "邮箱已验证,账号正在等待管理员审批。通过之后就能登录了。";
    case "account_rejected":
      return "这个账号没有通过审批。如有疑问请联系实验室管理员。";
    case "account_disabled":
      return "这个账号已被停用。请联系实验室管理员。";
    default:
      return "";
  }
});

function describe(e: unknown): void {
  if (e instanceof AuthError) {
    if (e.code.startsWith("account_")) {
      statusCode.value = e.code;
      error.value = "";
      return;
    }
    statusCode.value = "";
    // 429 与 503 的文案后端已经写清楚了(限流 / 缺配置),直接用 message。
    error.value = e.code === "rate_limited" ? "尝试太频繁了,请稍后再试。" : e.message;
    return;
  }
  statusCode.value = "";
  error.value = e instanceof Error ? e.message : String(e);
}

async function submit(): Promise<void> {
  if (busy.value) return;
  error.value = "";
  notice.value = "";
  statusCode.value = "";
  busy.value = true;
  try {
    await login(email.value.trim(), password.value);
    // 登录成功:清掉表单里的密码,交给 App 切到应用视图。
    password.value = "";
  } catch (e) {
    describe(e);
  } finally {
    busy.value = false;
  }
}

async function resend(): Promise<void> {
  const to = email.value.trim();
  if (!to) {
    error.value = "请先填写邮箱。";
    return;
  }
  busy.value = true;
  try {
    notice.value = await resendVerification(to);
  } catch (e) {
    describe(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div>
    <h1 class="auth__title">登录</h1>
    <p class="auth__lede">
      用注册时验证过的邮箱登录。账号需要管理员审批通过后才能使用。
    </p>

    <form novalidate @submit.prevent="submit">
      <div class="auth__field">
        <label class="auth__label" for="login-email">邮箱</label>
        <input
          id="login-email"
          v-model="email"
          class="auth__input"
          type="email"
          name="email"
          autocomplete="username"
          required
          :disabled="busy"
          placeholder="you@example.com"
        />
      </div>
      <div class="auth__field">
        <label class="auth__label" for="login-password">密码</label>
        <input
          id="login-password"
          v-model="password"
          class="auth__input"
          type="password"
          name="password"
          autocomplete="current-password"
          required
          :disabled="busy"
        />
      </div>

      <div class="auth__actions">
        <button class="auth__btn" type="submit" :disabled="busy || !email || !password">
          {{ busy ? "登录中…" : "登录" }}
        </button>
      </div>
    </form>

    <p v-if="error" class="auth__note auth__note--err" role="alert">{{ error }}</p>

    <!-- 上一次动作的结果(改密 / 退出全部设备 / 下线本设备):那些操作一做完界面就回到这里了,
         不说一句用户不知道自己为什么被登出。 -->
    <p v-if="authState.flash" class="auth__note auth__note--ok" role="status">{{ authState.flash }}</p>

    <!-- 账号状态说明 + 唯一能自助的那条出路(重发验证邮件) -->
    <div v-if="statusCode" class="auth__note auth__note--plain" role="status">
      {{ statusText }}
      <div v-if="statusCode === 'account_pending_email'" class="auth__actions">
        <button class="auth__btn auth__btn--ghost" type="button" :disabled="busy" @click="resend">
          重新发送验证邮件
        </button>
      </div>
    </div>

    <p v-if="notice" class="auth__note auth__note--ok" role="status">{{ notice }}</p>

    <div class="auth__meta">
      <button class="auth__link" type="button" @click="navigate('register')">注册新账号</button>
      <button class="auth__link auth__link--quiet" type="button" @click="navigate('forgot')">
        忘记密码
      </button>
    </div>

    <p v-if="authState.busy" class="auth__hint">正在恢复上次的登录状态…</p>
  </div>
</template>
