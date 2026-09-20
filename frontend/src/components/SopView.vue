<script setup lang="ts">
import { onErrorCaptured, onMounted, ref } from "vue";

import "../styles/sop.css";
import { useSops } from "../composables/useSops";
import SopDetail from "./SopDetail.vue";
import SopEditor from "./SopEditor.vue";
import SopList from "./SopList.vue";

const { mode, current, loaded, loadList, showList } = useSops();

// 进入视图即载入一次(单例已载过则保留列表,不重复请求)。
onMounted(() => {
  if (!loaded.value) void loadList();
});

// ── 错误边界 ──
// 接住三个子视图在渲染/生命周期/watcher 里抛出的同步异常,把「白屏」换成就地可读提示 + 恢复。
// 休眠态零开销:不拦 props/事件,只有子组件真的抛错时才醒来。
// 不返回 false —— 让异常继续冒泡到 main.ts 的全局 errorHandler,由它打印组件名与完整栈。
const renderFailed = ref(false);
const failMessage = ref("");
onErrorCaptured((err) => {
  renderFailed.value = true;
  failMessage.value = err instanceof Error ? err.message : String(err);
});

// 从错误态恢复:回到已知安全的列表态、清掉当前篇并重载,v-if 切换即重新挂载干净子树(无需刷新)。
function recover(): void {
  renderFailed.value = false;
  failMessage.value = "";
  current.value = null;
  showList();
  void loadList();
}
</script>

<template>
  <!-- 渲染出错兜底:整块内容不再是空白,给出成因 + 一键回到列表 -->
  <main v-if="renderFailed" class="sv">
    <div class="sv__wrap">
      <section class="sv__state">
        <p class="sv__stateHd">页面渲染出错</p>
        <p class="sv__stateBody">{{ failMessage || "发生未知错误。" }}</p>
        <button class="sv__retry" type="button" @click="recover">返回列表</button>
      </section>
    </div>
  </main>

  <template v-else>
    <SopEditor v-if="mode === 'editor'" />
    <SopDetail v-else-if="mode === 'detail'" />
    <SopList v-else />
  </template>
</template>
