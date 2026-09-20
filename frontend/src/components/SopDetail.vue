<script setup lang="ts">
import { computed } from "vue";

import { useSops } from "../composables/useSops";
import MarkdownView from "./MarkdownView.vue";

const { current, detailLoading, detailError, showList, openEditor } = useSops();

function formatWhen(sec: number | null): string {
  if (!sec) return "";
  const d = new Date(sec * 1000);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("zh-CN", { year: "numeric", month: "numeric", day: "numeric" });
}

// 更新于 X · 存于 sops/id.md;时间未知时只显存放位置。
const meta = computed(() => {
  const c = current.value;
  if (!c) return "";
  const when = formatWhen(c.updated_at);
  return when ? `更新于 ${when} · 存于 ${c.key}` : `存于 ${c.key}`;
});
</script>

<template>
  <main class="sv">
    <div class="sv__wrap">
      <button class="ed__back" type="button" @click="showList">
        <span aria-hidden="true">‹</span> 返回列表
      </button>

      <section v-if="detailLoading" class="sv__state sv__state--soft">
        <p class="sv__stateBody">载入中…</p>
      </section>

      <section v-else-if="detailError" class="sv__state">
        <p class="sv__stateHd">打开失败</p>
        <p class="sv__stateBody">{{ detailError }}</p>
        <button class="sv__retry" type="button" @click="showList">返回列表</button>
      </section>

      <article v-else-if="current" class="doc">
        <div class="doc__spine" />
        <div class="doc__body">
          <div class="doc__head">
            <div>
              <h2 class="doc__name">{{ current.name }}</h2>
              <code class="sop__id">{{ current.id }}</code>
            </div>
            <div class="doc__act">
              <button class="btn-ghost" type="button" @click="openEditor(current.id)">编辑</button>
            </div>
          </div>
          <p v-if="current.description" class="doc__desc">{{ current.description }}</p>
          <div v-if="current.triggers.length" class="doc__tags">
            <span v-for="t in current.triggers" :key="t" class="chip--static">{{ t }}</span>
          </div>
          <p class="doc__meta">{{ meta }}</p>
          <hr class="doc__rule" />
          <MarkdownView :source="current.body" />
        </div>
      </article>
    </div>
  </main>
</template>
