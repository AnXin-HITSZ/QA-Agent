<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, useId } from "vue";

defineProps<{ title: string; subtitle?: string }>();
const emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement | null>(null);
const closeButton = ref<HTMLButtonElement | null>(null);
const titleId = useId();
let previousFocus: HTMLElement | null = null;
let previousOverflow = "";

onMounted(() => {
  previousFocus = document.activeElement as HTMLElement | null;
  previousOverflow = document.body.style.overflow;
  dialog.value?.showModal();
  document.body.style.overflow = "hidden";
  closeButton.value?.focus({ preventScroll: true });
});
onBeforeUnmount(() => {
  dialog.value?.close();
  document.body.style.overflow = previousOverflow;
  if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
});
function dismissOutside(event: MouseEvent) {
  const element=dialog.value;
  if (!element || event.target !== element) return;
  const bounds = element.getBoundingClientRect();
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) emit("close");
}
</script>

<template>
  <Teleport to="body">
    <dialog ref="dialog" class="side-drawer" :aria-labelledby="titleId" @cancel.prevent="emit('close')" @click="dismissOutside">
      <header class="side-drawer__header">
        <div class="side-drawer__heading">
          <h2 :id="titleId">{{ title }} <slot name="badge" /></h2>
          <p v-if="subtitle">{{ subtitle }}</p>
        </div>
        <button ref="closeButton" type="button" class="side-drawer__close" aria-label="关闭任务详情" @click="emit('close')">
          <svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="m6 6 8 8M14 6l-8 8" /></svg>
        </button>
      </header>
      <div class="side-drawer__body"><slot /></div>
      <footer v-if="$slots.footer" class="side-drawer__footer"><slot name="footer" /></footer>
    </dialog>
  </Teleport>
</template>

<style scoped>
.side-drawer {position:fixed;inset:0 0 0 auto;box-sizing:border-box;width:min(680px,100vw);height:100dvh;max-width:100vw;max-height:100dvh;margin:0;padding:0;border:0;border-left:1px solid var(--line);background:var(--surface);color:var(--ink);box-shadow:-16px 0 48px #102a341a;overflow:hidden;}
.side-drawer[open] {display:flex;flex-direction:column;}
.side-drawer::backdrop {background:#102a3466;}
.side-drawer__header {display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:24px;border-bottom:1px solid var(--line);flex:none;}
.side-drawer__heading {min-width:0;}
h2 {display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0;font-size:16px;font-weight:600;}
.side-drawer__heading p {margin:8px 0 0;font:11px/1.6 "IBM Plex Mono",ui-monospace,monospace;color:var(--muted);overflow-wrap:anywhere;}
.side-drawer__close {display:grid;place-items:center;flex:none;width:30px;height:30px;padding:5px;border:1px solid var(--line);border-radius:50%;background:var(--surface);color:var(--muted);cursor:pointer;}
.side-drawer__close:hover {color:var(--ink);background:var(--surface-2);}
svg {width:18px;height:18px;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;}
.side-drawer__body {flex:1;min-height:0;overflow:auto;overscroll-behavior:contain;padding:24px;}
.side-drawer__footer {flex:none;border-top:1px solid var(--line);padding:14px 24px;background:var(--surface);}
button:focus-visible {outline:2px solid var(--focus);outline-offset:3px;}
@media(max-width:540px) {.side-drawer__header,.side-drawer__body {padding:18px;}.side-drawer__footer {padding:12px 18px;}}
</style>
