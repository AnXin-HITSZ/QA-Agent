<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";

import AppHeader from "./components/AppHeader.vue";
import ChatView from "./components/ChatView.vue";
import HistorySidebar from "./components/HistorySidebar.vue";
import KnowledgeView from "./components/KnowledgeView.vue";
import SopView from "./components/SopView.vue";
import { useSidebar } from "./composables/useSidebar";

type View = "chat" | "knowledge" | "sops";

// 历史抽屉开关(两视图共用;侧栏为固定覆盖层,不占布局 → 内容始终整窗居中)。
// 由对话视图的「会话工具胶囊」触发,故用模块级单例共享。
const { open, closeDrawer } = useSidebar();

// hash 路由:#/knowledge → 知识库,#/sops → SOP 流程,其它一律对话。刷新后停在当前视图,链接可分享。
function viewFromHash(): View {
  const h = location.hash.replace(/^#\/?/, "");
  if (h === "knowledge") return "knowledge";
  if (h === "sops") return "sops";
  return "chat";
}

const view = ref<View>(viewFromHash());

// 当前视图对应的组件(三视图映射,供 KeepAlive 保活)。
const viewComponent = computed(() => {
  if (view.value === "knowledge") return KnowledgeView;
  if (view.value === "sops") return SopView;
  return ChatView;
});

function changeView(v: View): void {
  view.value = v;
  closeDrawer(); // 切走时收起历史抽屉
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
  }
}

onMounted(() => {
  window.addEventListener("hashchange", onHashChange);
  // 首屏把 hash 规整到当前视图(不新增历史条目)。
  const h = "#/" + view.value;
  if (location.hash !== h) history.replaceState(null, "", h);
});
onUnmounted(() => window.removeEventListener("hashchange", onHashChange));
</script>

<template>
  <div class="app" :class="{ 'app--drawer': open }">
    <HistorySidebar class="app__side" @navigate="onSideNavigate" />
    <div class="app__backdrop" @click="closeDrawer" />

    <div class="app__main">
      <AppHeader :view="view" @change-view="changeView" />

      <!-- 三视图保活:切换即时,滚动位置 / 输入草稿 / 知识库当前分类 / SOP 浏览态都不丢 -->
      <Transition name="view-fade" mode="out-in">
        <KeepAlive>
          <component :is="viewComponent" />
        </KeepAlive>
      </Transition>
    </div>
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

/* 视图切换:一次极轻的淡入 */
.view-fade-enter-active,
.view-fade-leave-active {
  transition: opacity 0.18s ease;
}
.view-fade-enter-from,
.view-fade-leave-to {
  opacity: 0;
}

@media (prefers-reduced-motion: reduce) {
  .view-fade-enter-active,
  .view-fade-leave-active {
    transition: none;
  }
  .app__side {
    transition: none;
  }
}
</style>
