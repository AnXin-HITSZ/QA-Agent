<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";

import type { AppRouteName } from "../lib/route";
import { authState, isAdmin, logout, logoutAll } from "../stores/auth";
import { useTheme } from "../composables/useTheme";

defineProps<{ view: AppRouteName }>();
const emit = defineEmits<{
  (e: "change-view", view: AppRouteName): void;
}>();

const { theme, toggle } = useTheme();

const TABS: Array<{ name: AppRouteName; label: string }> = [
  { name: "chat", label: "对话" },
  { name: "knowledge", label: "知识库" },
  { name: "sops", label: "SOP 流程" },
  { name: "memory", label: "我的记忆" },
  { name: "metering", label: "调用与费用" },
];

// 调用与费用只对管理员开放:普通用户的标签行里直接没有它
// (直接输 hash 也会被 App.vue 的路由纠偏挡回对话)。
const tabs = computed(() => (isAdmin() ? TABS : TABS.filter((t) => t.name !== "metering")));

const me = computed(() => authState.user);
const initial = computed(() => (me.value?.display_name || me.value?.email || "?").slice(0, 1).toUpperCase());
const busy = ref(false);

// ── 账号菜单 ──
// 按钮 + 浮层:aria-expanded / aria-haspopup 给读屏,点外面或 Esc 关闭(键盘用户的常规出路)。
const menuOpen = ref(false);
const menuEl = ref<HTMLElement | null>(null);

function closeMenu(): void {
  menuOpen.value = false;
}

function onDocClick(e: MouseEvent): void {
  if (!menuOpen.value) return;
  if (menuEl.value && !menuEl.value.contains(e.target as Node)) closeMenu();
}

function onDocKeydown(e: KeyboardEvent): void {
  if (e.key === "Escape" && menuOpen.value) {
    e.stopPropagation(); // 别让 App 的 Esc 顺手把抽屉也收了:先关最上面这一层
    closeMenu();
  }
}

onMounted(() => {
  document.addEventListener("click", onDocClick);
  document.addEventListener("keydown", onDocKeydown, true);
});
onUnmounted(() => {
  document.removeEventListener("click", onDocClick);
  document.removeEventListener("keydown", onDocKeydown, true);
});

function goto(name: AppRouteName): void {
  closeMenu();
  emit("change-view", name);
}

async function signOut(all: boolean): Promise<void> {
  if (busy.value) return;
  closeMenu();
  busy.value = true;
  try {
    if (all) await logoutAll(); // 内部会清本地状态并把后端的话带上登录页
    else await logout();
  } catch {
    // 服务端失败也照样退出:两个函数内部都清了本地状态
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <header class="hd">
    <div class="hd__lead">
      <div class="hd__mark">
        <span class="hd__name">实验室问答</span>
        <span class="hd__sub">Lab Assistant</span>
      </div>
    </div>

    <div class="hd__right">
      <nav class="hd__tabs" aria-label="视图切换">
        <button
          v-for="tab in tabs"
          :key="tab.name"
          class="hd__tab"
          type="button"
          :class="{ 'is-active': view === tab.name }"
          :aria-current="view === tab.name ? 'page' : undefined"
          @click="emit('change-view', tab.name)"
        >
          {{ tab.label }}
        </button>
      </nav>

      <button
        class="hd__theme"
        type="button"
        :aria-label="theme === 'dark' ? '切换到亮色' : '切换到暗色'"
        @click="toggle"
      >
        {{ theme === "dark" ? "☀" : "☾" }}
      </button>

      <!-- 账号菜单 -->
      <div ref="menuEl" class="hd__me">
        <button
          class="hd__avatar"
          type="button"
          aria-haspopup="menu"
          :aria-expanded="menuOpen"
          :aria-label="`账号:${me?.display_name || me?.email || ''}`"
          @click="menuOpen = !menuOpen"
        >
          {{ initial }}
        </button>
        <div v-if="menuOpen" class="hd__menu" role="menu">
          <div class="hd__who">
            <div class="hd__who-name">{{ me?.display_name || "未命名" }}</div>
            <div class="hd__who-mail">{{ me?.email }}</div>
            <span class="badge" :class="isAdmin() ? 'badge--ok' : ''">
              {{ isAdmin() ? "管理员" : "普通用户" }}
            </span>
          </div>
          <button
            class="hd__item"
            type="button"
            role="menuitem"
            :class="{ 'is-active': view === 'account' }"
            @click="goto('account')"
          >
            我的账号
          </button>
          <button
            v-if="isAdmin()"
            class="hd__item"
            type="button"
            role="menuitem"
            :class="{ 'is-active': view === 'users' }"
            @click="goto('users')"
          >
            用户管理
          </button>
          <hr class="hd__sep" />
          <button class="hd__item" type="button" role="menuitem" :disabled="busy" @click="signOut(false)">
            退出登录
          </button>
          <button
            class="hd__item hd__item--danger"
            type="button"
            role="menuitem"
            :disabled="busy"
            @click="signOut(true)"
          >
            退出全部设备
          </button>
        </div>
      </div>
    </div>
  </header>
</template>

<style scoped>
.hd {
  display: flex;
  align-items: flex-end; /* 折页标签坐到页头底线上 */
  justify-content: space-between;
  gap: 16px;
  padding: 14px 24px 0;
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--surface) 72%, var(--paper));
  backdrop-filter: blur(6px);
  position: sticky;
  top: 0;
  z-index: 5;
}
.hd__lead {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 13px; /* 抬离底线,与标签视觉齐平 */
}
.hd__mark {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.hd__name {
  font-family: "Space Grotesk", "IBM Plex Sans", system-ui, sans-serif;
  font-weight: 600;
  font-size: 19px;
  letter-spacing: 0.01em;
  color: var(--ink);
}
.hd__sub {
  font-family: "Space Grotesk", sans-serif;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--muted);
  letter-spacing: 0.04em;
}

.hd__right {
  display: flex;
  align-items: flex-end;
  gap: 14px;
}
.hd__theme {
  flex-shrink: 0;
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  margin-bottom: 6px; /* 抬离底线,与标签视觉齐平 */
  border: 1px solid var(--line);
  border-radius: 50%;
  background: var(--surface);
  color: var(--ink);
  font-size: 15px;
  line-height: 1;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.hd__theme:hover {
  border-color: var(--primary);
  color: var(--primary);
}

/* 折页标签:活动标签白底(与内容区同色)、去下边框、压在底线上,连成一体 */
.hd__tabs {
  display: flex;
  align-items: flex-end;
  gap: 4px;
}
.hd__tab {
  padding: 9px 18px;
  border: 1px solid transparent;
  border-bottom: none;
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  margin-bottom: -1px; /* 压在页头底线上 */
  background: transparent;
  color: var(--muted);
  font: inherit;
  font-size: 14px;
  line-height: 1.4;
  cursor: pointer;
  white-space: nowrap;
  transition: color 0.15s, background 0.15s;
}
.hd__tab:hover {
  color: var(--ink);
}
.hd__tab.is-active {
  color: var(--ink);
  font-weight: 600;
  background: var(--paper); /* 与下方内容区同色 → 连成一体 */
  border-color: var(--line);
  box-shadow: inset 0 2px 0 var(--primary); /* 顶部一抹科研青,标明当前 */
}
.hd__tab:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: -2px;
}

/* ── 账号菜单 ── */
.hd__me {
  position: relative;
  flex-shrink: 0;
  margin-bottom: 6px; /* 与主题按钮同一基线 */
}
.hd__avatar {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border: 1px solid var(--line);
  border-radius: 50%;
  background: var(--primary-tint);
  color: var(--primary-strong);
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: border-color 0.15s;
}
.hd__avatar:hover {
  border-color: var(--primary);
}
.hd__menu {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  z-index: 30;
  min-width: 230px;
  padding: 6px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  box-shadow: var(--shadow);
}
.hd__who {
  padding: 8px 10px 10px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 6px;
}
.hd__who-name {
  font-weight: 600;
  color: var(--ink);
  font-size: 13.5px;
}
.hd__who-mail {
  color: var(--muted);
  font-size: 12px;
  overflow-wrap: anywhere;
  margin-bottom: 6px;
}
.hd__item {
  display: block;
  width: 100%;
  padding: 8px 10px;
  border: 0;
  border-radius: var(--radius-xs);
  background: none;
  color: var(--ink);
  font: inherit;
  font-size: 13.5px;
  text-align: left;
  cursor: pointer;
}
.hd__item:hover:not(:disabled) {
  background: var(--surface-2);
}
.hd__item:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.hd__item.is-active {
  color: var(--primary-strong);
  font-weight: 600;
}
.hd__item--danger {
  color: var(--seal);
}
.hd__sep {
  margin: 6px 4px;
  border: 0;
  border-top: 1px solid var(--line);
}

/* 窄屏:标签换行、页头两行也不挤 */
@media (max-width: 720px) {
  .hd {
    flex-wrap: wrap;
    align-items: flex-start;
    padding: 12px 14px 0;
    gap: 8px;
  }
  .hd__lead {
    padding-bottom: 0;
  }
  .hd__sub {
    display: none;
  }
  .hd__right {
    width: 100%;
    justify-content: space-between;
    align-items: center;
  }
  .hd__tabs {
    overflow-x: auto;
  }
  .hd__tab {
    padding: 8px 12px;
    font-size: 13px;
  }
  .hd__theme,
  .hd__me {
    margin-bottom: 6px;
  }
}
</style>
