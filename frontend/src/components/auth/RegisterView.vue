<script setup lang="ts">
// 注册。成功的文案是**中性**的(邮箱已存在也照样说「已发送」,不透露账号是否存在),
// 所以这里不假设「一定是新账号」,只说「如果这个邮箱可以注册……」。
// 密码策略与后端一致:8–128 个字符,任意字符,不做大小写数字组合要求。
import { ref } from "vue";

import { navigate } from "../../lib/route";
import { AuthError, register, resendVerification } from "../../stores/auth";

const email = ref("");
const displayName = ref("");
const password = ref("");
const confirm = ref("");
const busy = ref(false);
const error = ref("");
const done = ref("");
const notice = ref("");

async function submit(): Promise<void> {
  if (busy.value) return;
  error.value = "";
  notice.value = "";
  if (password.value !== confirm.value) {
    error.value = "两次输入的密码不一致。";
    return;
  }
  busy.value = true;
  try {
    done.value = await register(email.value.trim(), password.value, displayName.value.trim());
    password.value = "";
    confirm.value = "";
  } catch (e) {
    error.value = e instanceof AuthError ? e.message : e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

async function resend(): Promise<void> {
  busy.value = true;
  try {
    notice.value = await resendVerification(email.value.trim());
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div>
    <h1 class="auth__title">注册</h1>
    <p class="auth__lede">
      注册后需要两步才能使用:先点开邮件里的链接验证邮箱,再由管理员审批通过。
    </p>

    <template v-if="!done">
      <form novalidate @submit.prevent="submit">
        <div class="auth__field">
          <label class="auth__label" for="reg-email">邮箱</label>
          <input
            id="reg-email"
            v-model="email"
            class="auth__input"
            type="email"
            autocomplete="username"
            required
            :disabled="busy"
            placeholder="you@example.com"
          />
        </div>
        <div class="auth__field">
          <label class="auth__label" for="reg-name">昵称(可留空)</label>
          <input
            id="reg-name"
            v-model="displayName"
            class="auth__input"
            type="text"
            autocomplete="nickname"
            maxlength="64"
            :disabled="busy"
            placeholder="留空则用邮箱前缀"
          />
        </div>
        <div class="auth__field">
          <label class="auth__label" for="reg-pw">密码</label>
          <input
            id="reg-pw"
            v-model="password"
            class="auth__input"
            type="password"
            autocomplete="new-password"
            required
            :disabled="busy"
          />
          <p class="auth__hint">8–128 个字符,可用空格与中文;不强制大小写数字组合。</p>
        </div>
        <div class="auth__field">
          <label class="auth__label" for="reg-pw2">确认密码</label>
          <input
            id="reg-pw2"
            v-model="confirm"
            class="auth__input"
            type="password"
            autocomplete="new-password"
            required
            :disabled="busy"
          />
        </div>

        <div class="auth__actions">
          <button class="auth__btn" type="submit" :disabled="busy || !email || !password">
            {{ busy ? "提交中…" : "注册" }}
          </button>
        </div>
      </form>

      <p v-if="error" class="auth__note auth__note--err" role="alert">{{ error }}</p>
    </template>

    <template v-else>
      <p class="auth__note auth__note--ok" role="status">{{ done }}</p>
      <div class="auth__actions">
        <button class="auth__btn auth__btn--ghost" type="button" :disabled="busy" @click="resend">
          没收到?重新发送验证邮件
        </button>
      </div>
      <p v-if="notice" class="auth__note auth__note--plain" role="status">{{ notice }}</p>
    </template>

    <p v-if="error && done" class="auth__note auth__note--err" role="alert">{{ error }}</p>

    <div class="auth__meta">
      <button class="auth__link" type="button" @click="navigate('login')">已有账号,去登录</button>
    </div>
  </div>
</template>
