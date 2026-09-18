<script setup lang="ts">
import { computed } from "vue";

import { renderMarkdown } from "../lib/markdown";

const props = defineProps<{ source: string }>();

// renderMarkdown 用 html:false,输出仅 markdown-it 生成的标签,无注入面。
const html = computed(() => renderMarkdown(props.source));
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -->
  <div class="md" v-html="html" />
</template>
