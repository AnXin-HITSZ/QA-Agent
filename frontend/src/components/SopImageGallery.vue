<script setup lang="ts">
import { ref } from "vue";
import type { SopImageReference } from "../api";

defineProps<{ images: SopImageReference[] }>();
const failed = ref<Set<string>>(new Set());
function onError(id: string): void {
  failed.value = new Set(failed.value).add(id);
}
function retry(id: string): void {
  const next = new Set(failed.value);
  next.delete(id);
  failed.value = next;
}
</script>

<template>
  <section v-if="images.length" class="sop-images" aria-label="本次查阅的 SOP 图片">
    <p>本次查阅的 SOP 图片</p>
    <figure v-for="item in images" :key="item.image_id">
      <div v-if="failed.has(item.image_id)" class="sop-images__missing" role="status">
        <span>图片暂时无法加载或原件已不可用。</span>
        <button type="button" @click="retry(item.image_id)">重试</button>
      </div>
      <a v-else :href="item.url" target="_blank" rel="noopener noreferrer">
        <img :src="item.url" :alt="item.alt" loading="lazy" @error="onError(item.image_id)" />
      </a>
      <figcaption>{{ item.sop_name }} · {{ item.alt }}</figcaption>
    </figure>
  </section>
</template>

<style scoped>
.sop-images { margin-top: 20px; border-top: 1px solid var(--line); padding-top: 12px; }
.sop-images p, figcaption { color: var(--muted); font-size: 13px; }
figure { margin: 12px 0; }
img { display: block; max-width: 100%; max-height: 360px; object-fit: contain; }
figcaption { margin-top: 6px; }
.sop-images__missing { padding: 16px; background: var(--surface); }
button { margin-left: 10px; cursor: pointer; }
</style>
