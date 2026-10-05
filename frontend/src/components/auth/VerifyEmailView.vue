<script setup lang="ts">
// 邮箱验证结果页:邮件里的链接进来后**挂载即提交**(令牌是一次性的,不能等用户点按钮)。
// 三态:验证中 / 成功 / 失败。失败时给出路(去登录页重发验证邮件)。
// 令牌只在 hash 里(见 lib/route.ts),不发给服务器、不进访问日志。
import { onMounted, ref, watch } from "vue";

import { navigate } from "../../lib/route";
import { AuthError, verifyEmail } from "../../stores/auth";

const props = defineProps<{ route: { token: string } }>();

type Phase = "checking" | "ok" | "fail";
const phase = ref<Phase>("checking");
const message = ref("");

// 同址换令牌(或验证失败后原地重试)时,只认最后一次的结果,避免旧响应把新状态覆盖回去。
let seq = 0;

async function run(token: string): Promise<void> {
  const mine = ++seq;
  if (!token) {
    phase.value = "fail";
    message.value = "这个链接不完整(缺少验证令牌)。请从邮件里直接点开链接,或者到登录页重新发送验证邮件。";
    return;
  }
  phase.value = "checking";
  message.value = "";
  try {
    const ok = await verifyEmail(token);
    if (mine !== seq) return;
    message.value = ok;
    phase.value = "ok";
  } catch (e) {
    if (mine !== seq) return;
    phase.value = "fail";
    message.value = e instanceof AuthError ? e.message : e instanceof Error ? e.message : String(e);
  }
}

onMounted(() => void run(props.route.token));
watch(
  () => props.route.token,
  (token) => void run(token),
);
</script>

<template>
  <div>
    <h1 class="auth__title">邮箱验证</h1>

    <template v-if="phase === 'checking'">
      <p class="auth__lede">正在验证…</p>
      <p class="auth__spin" role="status">请稍候,不要重复点开这个链接。</p>
    </template>

    <template v-else-if="phase === 'ok'">
      <p class="auth__note auth__note--ok" role="status">{{ message }}</p>
      <p class="auth__lede auth__lede--tight">下一步:等管理员审批。审批通过后就能登录了。</p>
      <div class="auth__actions">
        <button class="auth__btn" type="button" @click="navigate('login')">去登录</button>
      </div>
    </template>

    <template v-else>
      <p class="auth__note auth__note--err" role="alert">{{ message }}</p>
      <div class="auth__actions">
        <button class="auth__btn auth__btn--ghost" type="button" @click="navigate('login')">
          去登录页重新发送验证邮件
        </button>
      </div>
    </template>
  </div>
</template>
