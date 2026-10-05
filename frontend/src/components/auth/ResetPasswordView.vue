<script setup lang="ts">
// 重置密码:令牌从邮件链接的 hash 里来(route.token),新密码与后端策略一致(8–128 字符)。
// 重置成功后后端会撤销该账号的**全部**会话,所以这里只有一条出路:回登录页重新登录。
import { computed, ref } from "vue";

import { navigate } from "../../lib/route";
import { AuthError, resetPassword } from "../../stores/auth";

const props = defineProps<{ route: { token: string } }>();

const password = ref("");
const confirm = ref("");
const busy = ref(false);
const error = ref("");
const done = ref("");

const hasToken = computed(() => props.route.token.length > 0);

async function submit(): Promise<void> {
  if (busy.value) return;
  error.value = "";
  if (password.value !== confirm.value) {
    error.value = "两次输入的密码不一致。";
    return;
  }
  busy.value = true;
  try {
    done.value = await resetPassword(props.route.token, password.value);
    password.value = "";
    confirm.value = "";
  } catch (e) {
    error.value = e instanceof AuthError ? e.message : e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div>
    <h1 class="auth__title">重置密码</h1>

    <template v-if="!done">
      <p class="auth__lede">
        设置一个新密码。重置成功后所有设备都需要用新密码重新登录。
      </p>

      <p v-if="!hasToken" class="auth__note auth__note--err" role="alert">
        这个链接不完整(缺少重置令牌)。请从邮件里直接点开链接,或重新发一封重置邮件。
      </p>

      <form novalidate @submit.prevent="submit">
        <div class="auth__field">
          <label class="auth__label" for="reset-pw">新密码</label>
          <input
            id="reset-pw"
            v-model="password"
            class="auth__input"
            type="password"
            autocomplete="new-password"
            required
            :disabled="busy || !hasToken"
          />
          <p class="auth__hint">8–128 个字符,可用空格与中文;不强制大小写数字组合。</p>
        </div>
        <div class="auth__field">
          <label class="auth__label" for="reset-pw2">确认新密码</label>
          <input
            id="reset-pw2"
            v-model="confirm"
            class="auth__input"
            type="password"
            autocomplete="new-password"
            required
            :disabled="busy || !hasToken"
          />
        </div>
        <div class="auth__actions">
          <button class="auth__btn" type="submit" :disabled="busy || !hasToken || !password">
            {{ busy ? "提交中…" : "重置密码" }}
          </button>
        </div>
      </form>

      <p v-if="error" class="auth__note auth__note--err" role="alert">{{ error }}</p>
    </template>

    <template v-else>
      <p class="auth__note auth__note--ok" role="status">{{ done }}</p>
      <div class="auth__actions">
        <button class="auth__btn" type="button" @click="navigate('login')">去登录</button>
      </div>
    </template>

    <div class="auth__meta">
      <button class="auth__link" type="button" @click="navigate('login')">返回登录</button>
      <button class="auth__link auth__link--quiet" type="button" @click="navigate('forgot')">
        重新发一封重置邮件
      </button>
    </div>
  </div>
</template>
