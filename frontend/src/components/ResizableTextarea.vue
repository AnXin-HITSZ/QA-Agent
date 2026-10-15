<script setup lang="ts">
import { ref, useId } from "vue";

defineProps<{ modelValue: string; placeholder?: string; required?: boolean; label?: string }>();
const emit = defineEmits<{ "update:modelValue": [value: string] }>();
const id = useId();
const height = ref(84);
const dragging = ref(false);
const minimum = 72;
const maximum = 480;
let startY = 0;
let startHeight = 0;
function resize(value: number) {height.value = Math.max(minimum, Math.min(maximum, value));}
function start(event: PointerEvent) {
  if (event.button !== 0) return;
  event.preventDefault();
  dragging.value = true;startY=event.clientY;startHeight=height.value;
  (event.currentTarget as HTMLElement).focus({preventScroll:true});
  (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
}
function move(event: PointerEvent) {if (dragging.value) resize(startHeight+event.clientY-startY);}
function stop(event: PointerEvent) {
  dragging.value = false;
  const handle=event.currentTarget as HTMLElement;
  if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
}
function keyboard(event: KeyboardEvent) {
  if (!['ArrowUp','ArrowDown','Home','End'].includes(event.key)) return;
  event.preventDefault();
  resize(event.key==='Home' ? minimum : event.key==='End' ? maximum : height.value+(event.key==='ArrowDown' ? 24 : -24));
}
</script>

<template>
  <span class="resizable-field" :class="{'is-resizing':dragging}" :style="{height:height+'px'}">
    <textarea :id="id" :aria-label="label" :value="modelValue" :placeholder="placeholder" :required="required" rows="2" @input="emit('update:modelValue',($event.target as HTMLTextAreaElement).value)" />
    <span class="resizable-field__handle" role="separator" tabindex="0" aria-label="调整关键词输入框高度" aria-orientation="horizontal" :aria-controls="id" :aria-valuemin="minimum" :aria-valuemax="maximum" :aria-valuenow="height" :aria-valuetext="`${height} 像素`" title="拖动调整高度，也可使用上下方向键" @pointerdown="start" @pointermove="move" @pointerup="stop" @pointercancel="stop" @lostpointercapture="dragging=false" @keydown="keyboard" @click.prevent>
      <svg viewBox="0 0 20 16" fill="none" aria-hidden="true"><path d="M5 5h10M5 10h10" /></svg>
    </span>
  </span>
</template>

<style scoped>
.resizable-field {position:relative;display:block;min-width:0;}
textarea {display:block;box-sizing:border-box;width:100%;height:100%;margin:0;padding:11px 44px 11px 12px;border:1px solid var(--line);border-radius:var(--radius-xs);background:var(--surface);color:var(--ink);font:inherit;line-height:1.6;resize:none;transition:border-color .15s;}
textarea:hover {border-color:color-mix(in srgb,var(--primary) 35%,var(--line));}
textarea:focus-visible {outline:2px solid var(--focus);outline-offset:2px;}
.resizable-field__handle {position:absolute;right:6px;bottom:6px;display:grid;place-items:center;box-sizing:border-box;width:28px;height:24px;border:1px solid var(--line);border-radius:5px;background:var(--surface-2);color:var(--muted);cursor:ns-resize;touch-action:none;user-select:none;}
.resizable-field__handle:hover,.resizable-field__handle:focus-visible,.is-resizing .resizable-field__handle {color:var(--primary-strong);background:var(--primary-tint);border-color:var(--primary);}
.resizable-field__handle:focus-visible {outline:2px solid var(--focus);outline-offset:2px;}
svg {width:18px;height:16px;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;pointer-events:none;}
@media(prefers-reduced-motion:reduce) {textarea {transition:none;}}
</style>
