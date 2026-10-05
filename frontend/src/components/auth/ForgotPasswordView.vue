<script setup lang="ts">
// 找回密码:只负责把重置邮件发出去。成功的文案是**中性**的(邮箱没注册过也照样说「已发送」),
// 所以这里不确认「账号存在」,只让用户去看邮箱。
import { ref } from "vue";

import { navigate } from "../../lib/route";
import { AuthError, forgotPassword } from "../../stores/auth";

const email = ref("");
const busy = ref(false);
const error = ref("");
const done = ref("");

async function submit(): Promise<void> {
  if (busy.value || !email.value.trim()) return;
  error.value = "";
  busy.value = true;
  try {
    done.value = await forgotPassword(email.value.trim());
  } catch (e) {
    error.value = e instanceof AuthError ? e.message : e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div>
    <h1 class="auth__title">找回密码</h1>
    <p class="auth__lede">填注册时用的邮箱,我们会发一封重置密码的邮件。链接有时效,用过就失效。</p>

    <template v-if="!done">
      <form novalidate @submit.prevent="submit">
        <div class="auth__field">
          <label class="auth__label" for="forgot-email">邮箱</label>
          <input
            id="forgot-email"
            v-model="email"
            class="auth__input"
            type="email"
            autocomplete="username"
            required
            :disabled="busy"
            placeholder="you@example.com"
          />
        </div>
        <div class="auth__actions">
          <button class="auth__btn" type="submit" :disabled="busy || !email">
            {{ busy ? "发送中…" : "发送重置邮件" }}
          </button>
        </div>
      </form>
      <p v-if="error" class="auth__note auth__note--err" role="alert">{{ error }}</p>
    </template>

    <template v-else>
      <p class="auth__note auth__note--ok" role="status">{{ done }}</p>
      <p class="auth__hint">
        没收到?确认邮箱拼写、看看垃圾邮件;邮件可能延迟几分钟再点一次。反复收不到请联系实验室管理员。
      </p>
    </template>

    <div class="auth__meta">
      <button class="auth__link" type="button" @click="navigate('login')">返回登录</button>
    </div>
  </div>
</template>
