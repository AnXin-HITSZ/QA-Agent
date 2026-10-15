<script setup lang="ts">
defineProps<{ label: string }>();
defineEmits<{ clear: [] }>();
</script>

<template>
  <button class="search-clear" type="button" :aria-label="label" @click="$emit('clear')">
    <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8" /></svg>
  </button>
</template>

<style scoped>
/* Match the existing memory search clear control. */
.search-clear {position:absolute;right:7px;top:50%;transform:translateY(-50%);width:26px;height:26px;padding:0;display:grid;place-items:center;border:0;border-radius:5px;background:transparent;color:var(--muted);cursor:pointer;}
.search-clear svg {width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:1.6;}
.search-clear:hover {background:var(--primary-tint);color:var(--primary);}
.search-clear:focus-visible {outline:2px solid var(--focus);outline-offset:1px;}
</style>
