<script setup lang="ts">
// 未登录时的外壳:一张居中卡片 + 品牌标记,里面按路由渲染具体的认证页。
// 认证页之间用 hash 路由跳转(见 lib/route.ts),各自不重复画卡片。
import { computed } from "vue";

import type { Route } from "../../lib/route";
import ForgotPasswordView from "./ForgotPasswordView.vue";
import LoginView from "./LoginView.vue";
import RegisterView from "./RegisterView.vue";
import ResetPasswordView from "./ResetPasswordView.vue";
import VerifyEmailView from "./VerifyEmailView.vue";

const props = defineProps<{ route: Route }>();

const view = computed(() => {
  switch (props.route.name) {
    case "register":
      return RegisterView;
    case "verify":
      return VerifyEmailView;
    case "forgot":
      return ForgotPasswordView;
    case "reset":
      return ResetPasswordView;
    default:
      return LoginView; // 未登录时停在应用内路由(如直接开 #/knowledge)→ 回登录页
  }
});
</script>

<template>
  <div class="auth">
    <div class="auth__card">
      <div class="auth__mark">
        <span class="auth__name">实验室问答</span>
        <span class="auth__sub">Lab Assistant</span>
      </div>
      <component :is="view" :route="route" />
    </div>
  </div>
</template>
