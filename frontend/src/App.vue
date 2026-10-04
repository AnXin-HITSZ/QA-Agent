<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";

import AppHeader from "./components/AppHeader.vue";
import ChatView from "./components/ChatView.vue";
import HistorySidebar from "./components/HistorySidebar.vue";
import KnowledgeView from "./components/KnowledgeView.vue";
import MeteringView from "./components/MeteringView.vue";
import SopView from "./components/SopView.vue";
import TodoDrawer from "./components/TodoDrawer.vue";
import { useSidebar } from "./composables/useSidebar";
import { useTodoDrawer } from "./composables/useTodoDrawer";

type View = "chat" | "knowledge" | "sops" | "metering";

// 历史抽屉开关(两视图共用;侧栏为固定覆盖层,不占布局 → 内容始终整窗居中)。
// 由对话视图的「会话工具胶囊」触发,故用模块级单例共享。
const { open, closeDrawer } = useSidebar();
// 右侧待办抽屉开关(与左侧对称);切视图时一并收起。
const { open: todoOpen, close: closeTodo } = useTodoDrawer();

// hash 路由:#/knowledge → 知识库,#/sops → SOP 流程,#/metering → 调用与费用,其它一律对话。
// 刷新后停在当前视图,链接可分享。
function viewFromHash(): View {
  const h = location.hash.replace(/^#\/?/, "");
  if (h === "knowledge") return "knowledge";
  if (h === "sops") return "sops";
  if (h === "metering") return "metering";
  return "chat";
}

const view = ref<View>(viewFromHash());

// 当前视图对应的组件(四视图映射,供 KeepAlive 保活)。
const viewComponent = computed(() => {
  if (view.value === "knowledge") return KnowledgeView;
  if (view.value === "sops") return SopView;
  if (view.value === "metering") return MeteringView;
  return ChatView;
});

function changeView(v: View): void {
  view.value = v;
  closeDrawer(); // 切走时收起历史抽屉
  closeTodo(); // 一并收起待办抽屉
  const h = "#/" + v;
  if (location.hash !== h) location.hash = h; // 写入历史,浏览器前进/后退可用
}

// 从历史抽屉里「新对话 / 打开某通」:收起抽屉,并确保回到对话视图。
function onSideNavigate(): void {
  closeDrawer();
  if (view.value !== "chat") changeView("chat");
}

// 浏览器前进/后退(或手改 hash)时同步视图。
function onHashChange(): void {
  const v = viewFromHash();
  if (v !== view.value) {
    view.value = v;
    closeDrawer();
    closeTodo();
  }
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
  // 首屏把 hash 规整到当前视图(不新增历史条目)。
  const h = "#/" + view.value;
  if (location.hash !== h) history.replaceState(null, "", h);
});
onUnmounted(() => {
  window.removeEventListener("hashchange", onHashChange);
  window.removeEventListener("keydown", onKeydown);
});
</script>

<template>
  <div class="app" :class="{ 'app--drawer': open, 'app--todo': todoOpen }">
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
      <AppHeader :view="view" @change-view="changeView" />

      <!-- 四视图保活:切换即时,滚动位置 / 输入草稿 / 知识库当前分类 / SOP 浏览态都不丢。
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
