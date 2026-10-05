<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";

import AppHeader from "./components/AppHeader.vue";
import ChatView from "./components/ChatView.vue";
import HistorySidebar from "./components/HistorySidebar.vue";
import KnowledgeView from "./components/KnowledgeView.vue";
import MeteringView from "./components/MeteringView.vue";
import SopView from "./components/SopView.vue";
import TodoDrawer from "./components/TodoDrawer.vue";
import AccountView from "./components/auth/AccountView.vue";
import AdminUsersView from "./components/auth/AdminUsersView.vue";
import AuthShell from "./components/auth/AuthShell.vue";
import { useChat } from "./composables/useChat";
import { useKnowledge } from "./composables/useKnowledge";
import { useMetering } from "./composables/useMetering";
import { useSidebar } from "./composables/useSidebar";
import { useSops } from "./composables/useSops";
import { useTodoCategories } from "./composables/useTodoCategories";
import { useTodoDrawer } from "./composables/useTodoDrawer";
import { useTodos } from "./composables/useTodos";
import {
  href,
  isAppRoute,
  isAuthRoute,
  normalizeEntry,
  parseRoute,
  type AppRouteName,
  type Route,
  type RouteName,
} from "./lib/route";
import { authState, bootstrap, isAdmin, onSignedOut } from "./stores/auth";

// 历史抽屉开关(两视图共用;侧栏为固定覆盖层,不占布局 → 内容始终整窗居中)。
// 由对话视图的「会话工具胶囊」触发,故用模块级单例共享。
const { open, closeDrawer } = useSidebar();
// 右侧待办抽屉开关(与左侧对称);切视图时一并收起。
const { open: todoOpen, close: closeTodo } = useTodoDrawer();

// ── 登出 / 换账号:清空全部用户级前端单例 ──
// 这几个 composable 都是模块级单例,跨登录一直活着;KeepAlive 缓存随登录外壳销毁,但模块状态
// 不会。不清就会串号:下一个账号挂载时带着上一个账号的对话列表 / 分类树 / 待办 / 「已载入」标记。
// 注册点放这里(App 挂一次),免得 stores/auth 反向依赖各个 composable。
const chat = useChat();
const knowledge = useKnowledge();
const sops = useSops();
const metering = useMetering();
const todos = useTodos();
const todoCategories = useTodoCategories();

onSignedOut(() => {
  chat.reset();
  knowledge.reset();
  sops.reset();
  metering.reset();
  todos.reset();
  todoCategories.reset();
  closeDrawer();
  closeTodo();
});

// ── 路由 ──
// 登录态由后端说了算(store 里内存态 + HttpOnly 刷新 cookie):首屏先静默换一枚 access token,
// 换到了就进应用,换不到(401)才停在登录页。首屏之前不渲染任何一边 —— 否则已登录的人每次
// 刷新都会先闪一下登录表单。
normalizeEntry(); // 邮件链接的路径形式 → hash(见 lib/route.ts)
const route = ref<Route>(parseRoute());

// 当前视图对应的组件(供 KeepAlive 保活)。
const viewComponent = computed(() => {
  switch (route.value.name) {
    case "knowledge":
      return KnowledgeView;
    case "sops":
      return SopView;
    case "metering":
      return MeteringView;
    case "account":
      return AccountView;
    case "users":
      return AdminUsersView;
    default:
      return ChatView;
  }
});

// 页头标签高亮用的视图名:认证页(未登录那一屏)不算应用内视图。
const activeView = computed<AppRouteName>(() =>
  isAppRoute(route.value.name) ? route.value.name : "chat",
);

function applyHash(): void {
  const next = parseRoute();
  if (next.name === route.value.name && next.token === route.value.token) return;
  route.value = next;
  closeDrawer(); // 切走时收起两个抽屉
  closeTodo();
  settle();
}

function go(name: RouteName, params: Record<string, string> = {}): void {
  const next = href(name, params);
  if (location.hash === next) {
    applyHash(); // 同址重进(如验证失败后原地重试):手动同步一次
    return;
  }
  location.hash = next; // 触发 hashchange → applyHash
}

// 登录态与路由对不上时纠偏,让地址栏和屏幕上是同一件事:
// 已登录却停在认证页(或非管理员停在用户管理)→ 回对话;未登录却停在应用内路由 → 回登录页。
// 前端这一层只是省得看到不相干的界面;**权限本身由后端把关**(见 docs 技术方案 §6)。
function settle(): void {
  if (authState.user) {
    if (isAuthRoute(route.value.name) || (route.value.name === "users" && !isAdmin())) go("chat");
    return;
  }
  if (!isAuthRoute(route.value.name)) go("login");
}

// 首屏恢复完成、以及之后每次登录 / 登出。user 每次都是整体替换,引用变了才触发。
watch([() => authState.ready, () => authState.user], () => settle());

// 从历史抽屉里「新对话 / 打开某通」:收起抽屉,并确保回到对话视图(已在对话页时也要收抽屉)。
function onSideNavigate(): void {
  closeDrawer();
  if (route.value.name !== "chat") go("chat");
}

// 浏览器前进/后退(或手改 hash)时同步视图。
function onHashChange(): void {
  applyHash();
}

// ── 抽屉可达性 ──
// 抽屉只是被 translate 推到屏外,内容仍在 DOM 里可聚焦:键盘一路 Tab 会走进看不见的抽屉,
// 读屏也会念到它们。关着时整块 inert + aria-hidden,开着时把焦点请进去、关掉再还给触发键。
const sideEl = ref<{ $el: HTMLElement } | null>(null);
const todoEl = ref<{ $el: HTMLElement } | null>(null);
let lastFocused: HTMLElement | null = null;

async function syncDrawer(isOpen: boolean, drawer: { $el: HTMLElement } | null): Promise<void> {
  if (isOpen) {
    lastFocused = (document.activeElement as HTMLElement | null) ?? null;
    await nextTick(); // 等 inert 摘掉、抽屉滑到位
    const el = drawer?.$el;
    // 若抽屉内部已自己拿到焦点(如「搜索」把光标送进输入框),不去抢。
    if (el && !el.contains(document.activeElement)) el.focus({ preventScroll: true });
    return;
  }
  const back = lastFocused;
  lastFocused = null;
  // 触发键可能已随视图切走 / 被重建;还挂在文档上才还焦点。
  if (back?.isConnected) back.focus({ preventScroll: true });
}

watch(open, (isOpen) => void syncDrawer(isOpen, sideEl.value));
watch(todoOpen, (isOpen) => void syncDrawer(isOpen, todoEl.value));

// Esc 收起抽屉:键盘用户除「再点一次」之外的另一条出口。
function onKeydown(e: KeyboardEvent): void {
  if (e.key !== "Escape" || e.defaultPrevented) return;
  if (open.value) closeDrawer();
  else if (todoOpen.value) closeTodo();
}

onMounted(() => {
  window.addEventListener("hashchange", onHashChange);
  window.addEventListener("keydown", onKeydown);
  // 首屏把 hash 规整成标准形式(不新增历史条目)。带令牌的邮件链接原样留着 ——
  // 令牌只在 hash 里,重写掉就等于把用户手上的链接弄丢。
  if (!route.value.token) {
    const h = href(route.value.name === "users" && !isAdmin() ? "chat" : route.value.name);
    if (location.hash !== h) history.replaceState(null, "", h);
  }

  // 静默恢复登录态。**不 await**:界面先按「恢复中」渲染,拿到结果后 watch 里 settle 切换。
  void bootstrap();
});
onUnmounted(() => {
  window.removeEventListener("hashchange", onHashChange);
  window.removeEventListener("keydown", onKeydown);
});
</script>

<template>
  <!-- 首屏:还在静默换令牌。什么都不给(不是登录页,也不是应用) -->
  <div v-if="!authState.ready" class="boot" role="status">
    <span class="boot__spin">正在恢复登录状态…</span>
  </div>

  <!-- 未登录:一张登录卡片;已登录:应用本体 -->
  <AuthShell v-else-if="!authState.user" :route="route" />

  <div v-else class="app" :class="{ 'app--drawer': open, 'app--todo': todoOpen }">
    <!-- id / tabindex 落到抽屉根 <aside> 上:前者给工具键的 aria-controls,后者让抽屉能承接焦点 -->
    <HistorySidebar
      id="history-drawer"
      ref="sideEl"
      class="app__side"
      tabindex="-1"
      :inert="!open"
      :aria-hidden="!open"
      @navigate="onSideNavigate"
    />
    <div class="app__backdrop" @click="closeDrawer" />

    <div class="app__main">
      <AppHeader :view="activeView" @change-view="go" />

      <!-- 六视图保活:切换即时,滚动位置 / 输入草稿 / 知识库当前分类 / SOP 浏览态都不丢。
           注意:此处曾用 <Transition mode="out-in"> 包裹,但它与 <KeepAlive> 组合会触发
           Vue 3.5.3+ 的已知回归(vuejs/core#12653):生产构建下依次逛过三个视图后回到
           首个视图会整块白屏(dev 模式不复现)。故去掉过渡、只留 KeepAlive,切换改为即时。 -->
      <KeepAlive>
        <component :is="viewComponent" />
      </KeepAlive>
    </div>

    <!-- 右侧待办抽屉(与左侧历史侧栏对称:固定覆盖层,不占布局)。常驻 DOM →
         应用启动即拉一次待办,对话视图工具键的未完成徽标随之有数。 -->
    <TodoDrawer
      id="todo-drawer"
      ref="todoEl"
      class="app__todo"
      tabindex="-1"
      :inert="!todoOpen"
      :aria-hidden="!todoOpen"
    />
    <div class="app__todo-backdrop" @click="closeTodo" />
  </div>
</template>

<style scoped>
/* 首屏占位:与未登录卡片同一底色,避免「白屏 → 登录页 → 应用」三段闪 */
.boot {
  min-height: 100vh;
  min-height: 100dvh;
  display: grid;
  place-items: center;
  background: var(--paper);
}
.boot__spin {
  color: var(--muted);
  font-size: 13px;
}

.app {
  display: flex;
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
}
.app__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

/* 历史侧栏:所有宽度都是滑出抽屉、不占布局 → 两视图内容都整窗居中,切换零横移 */
.app__side {
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  width: 260px;
  z-index: 20;
  transform: translateX(-100%);
  transition: transform 0.22s ease;
  box-shadow: var(--shadow);
}
.app--drawer .app__side {
  transform: translateX(0);
}
.app__backdrop {
  display: none;
}
.app--drawer .app__backdrop {
  display: block;
  position: fixed;
  inset: 0;
  z-index: 15;
  background: color-mix(in srgb, var(--ink) 32%, transparent);
}

/* 待办侧栏:镜像左侧,从右滑出、不占布局 → 内容始终整窗居中,切换零横移 */
.app__todo {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: 328px;
  max-width: 86vw;
  z-index: 20;
  transform: translateX(100%);
  transition: transform 0.22s ease;
  box-shadow: var(--shadow);
}
.app--todo .app__todo {
  transform: translateX(0);
}
.app__todo-backdrop {
  display: none;
}
.app--todo .app__todo-backdrop {
  display: block;
  position: fixed;
  inset: 0;
  z-index: 15;
  background: color-mix(in srgb, var(--ink) 32%, transparent);
}

@media (prefers-reduced-motion: reduce) {
  .app__side,
  .app__todo {
    transition: none;
  }
}
</style>
