<script setup lang="ts">
// 五选一「提取方式」+ 随模式出现的选项(混贴票据页 / 忽略缓存)。
// 状态在 useKnowledge 里是模块级共享的;现在只有工具条渲染它(索引任务面板那份已移除,
// 两处同选、看不出哪份生效的困惑随之消失)。
// 首次索引前必须显式选择,不替用户隐式决定(native_only 之外都会调用付费 OCR)。
import { useKnowledge } from "../composables/useKnowledge";

const { EXTRACTION_MODES, extractionMode, mixedInvoice, refreshOcr } = useKnowledge();
</script>

<template>
  <fieldset class="mp">
    <legend class="mp__legend">
      提取方式<span v-if="!extractionMode" class="mp__req">· 首次索引前请选择</span>
    </legend>

    <div class="mp__opts">
      <label
        v-for="m in EXTRACTION_MODES"
        :key="m.value"
        class="mp__opt"
        :class="{ 'is-on': extractionMode === m.value }"
      >
        <input
          v-model="extractionMode"
          class="mp__radio"
          type="radio"
          name="extraction-mode"
          :value="m.value"
        />
        <span class="mp__text">
          <span class="mp__label">{{ m.label }}</span>
          <span class="mp__hint">{{ m.hint }}</span>
        </span>
      </label>
    </div>

    <div class="mp__extra">
      <label v-if="extractionMode === 'invoice'" class="mp__check">
        <input v-model="mixedInvoice" type="checkbox" />
        <span>混贴票据页</span>
        <span class="mp__hint">一页里混贴多种票据时按混贴模式识别</span>
      </label>
      <label
        class="mp__check"
        :class="{ 'is-off': !extractionMode || extractionMode === 'native_only' }"
      >
        <input
          v-model="refreshOcr"
          type="checkbox"
          :disabled="!extractionMode || extractionMode === 'native_only'"
        />
        <span>忽略缓存重新识别</span>
        <span class="mp__hint">重新调用云端 OCR 并重新计费</span>
      </label>
    </div>
  </fieldset>
</template>

<style scoped>
.mp {
  margin: 0 0 12px;
  padding: 10px 12px 11px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
}
.mp__legend {
  padding: 0 6px;
  color: var(--muted);
  font-size: 12px;
}
.mp__req {
  color: var(--primary);
}
/* 五张卡片等宽铺满整行(auto-fit:宽度不够时按 190px 下限换行,余下几张仍拉满该行) */
.mp__opts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 6px;
}
.mp__opt {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  min-width: 0;
  padding: 7px 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.mp__opt:hover {
  border-color: var(--primary);
}
.mp__opt.is-on {
  border-color: var(--primary);
  background: var(--primary-tint);
}
.mp__radio {
  margin: 3px 0 0;
  accent-color: var(--primary);
  flex-shrink: 0;
}
.mp__text {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}
.mp__label {
  color: var(--ink);
  font-size: 13px;
  line-height: 1.4;
}
.mp__opt.is-on .mp__label {
  color: var(--primary-strong);
}
.mp__hint {
  color: var(--muted);
  font-size: 11.5px;
  line-height: 1.5;
}
.mp__extra {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin-top: 9px;
}
.mp__check {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  color: var(--ink);
  font-size: 12.5px;
  cursor: pointer;
}
.mp__check.is-off {
  opacity: 0.55;
  cursor: not-allowed;
}
.mp__check input {
  accent-color: var(--primary);
}
</style>
