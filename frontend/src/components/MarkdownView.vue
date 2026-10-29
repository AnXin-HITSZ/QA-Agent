<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from "vue";
import { apiFetch } from "../stores/auth";
import { renderMarkdown } from "../lib/markdown";
const props = defineProps<{ source: string }>();
const html = computed(() => renderMarkdown(props.source));
const root = ref<HTMLElement>();
const stop = watch([html, root], async (_, __, onCleanup) => {
  const controller = new AbortController();
  const urls: string[] = [];
  onCleanup(() => {
    controller.abort();
    urls.forEach((url) => URL.revokeObjectURL(url));
  });
  await Promise.all(Array.from(root.value?.querySelectorAll<HTMLImageElement>("img") ?? [], async (img) => {
    const src = img.getAttribute("src") ?? "";
    // Never send credentials to arbitrary Markdown image URLs.
    if (!/^\/api\/v1\/sops\/images\/[a-f0-9]{32}\.(png|jpg|gif|webp)$/.test(src)) return;
    img.removeAttribute("src");
    try {
      const response = await apiFetch(src, { signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      urls.push(url);
      img.src = url;
    } catch {
      if (!controller.signal.aborted) img.alt = "图片加载失败，请刷新重试";
    }
  }));
}, { immediate: true, flush: "post" });
onBeforeUnmount(stop);
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -->
  <div ref="root" class="md" v-html="html" />
</template>
