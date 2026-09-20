<script setup lang="ts">
import { computed, ref } from "vue";

import { useSops } from "../composables/useSops";

const { list, loading, error, storageEnabled, openDetail, openEditor, loadList, remove } = useSops();

// 行内删除确认:同一时刻只有一张卡处于确认态(键为 SOP id)。
const confirmId = ref<string | null>(null);
const deleting = ref<string | null>(null);
const opError = ref("");

const count = computed(() => list.value.length);

function msg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

async function onDelete(id: string): Promise<void> {
  confirmId.value = null;
  deleting.value = id;
  opError.value = "";
  try {
    await remove(id);
  } catch (e) {
    opError.value = msg(e);
  } finally {
    deleting.value = null;
  }
}

function formatWhen(sec: number | null): string {
  if (!sec) return "";
  const d = new Date(sec * 1000);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("zh-CN", { year: "numeric", month: "numeric", day: "numeric" });
}
</script>

<template>
  <main class="sv">
    <div class="sv__wrap">
      <div class="sv__bar">
        <div>
          <h2 class="sv__title">SOP 流程</h2>
          <p class="sv__count">
            <template v-if="loading">载入中…</template>
            <template v-else-if="storageEnabled && count">
              共 {{ count }} 篇流程 · 存于 OSS「sops/」前缀
            </template>
            <template v-else-if="storageEnabled">存于 OSS「sops/」前缀</template>
          </p>
        </div>
        <button
          v-if="storageEnabled"
          class="btn-primary"
          type="button"
          @click="openEditor()"
        >
          <span aria-hidden="true">＋</span> 新建 SOP
        </button>
      </div>

      <!-- 存储未接通:走空态,给配置方向,不当报错 -->
      <section v-if="!storageEnabled" class="sv__state">
        <p class="sv__stateHd">SOP 存储未接通</p>
        <p class="sv__stateBody">
          在 <code>backend/.env</code> 配置阿里云 OSS(私有桶 + RAM 最小权限)后重试。
        </p>
        <button class="sv__retry" type="button" @click="loadList">重试</button>
      </section>

      <!-- 载入失败 -->
      <section v-else-if="error" class="sv__state">
        <p class="sv__stateHd">载入失败</p>
        <p class="sv__stateBody">{{ error }}</p>
        <button class="sv__retry" type="button" @click="loadList">重试</button>
      </section>

      <!-- 空:一篇都没有 -->
      <section v-else-if="!loading && !count" class="sv__state sv__state--soft">
        <p class="sv__stateHd">还没有 SOP 流程</p>
        <p class="sv__stateBody">用右上角「新建 SOP」写下第一篇报销 / 办事流程。</p>
      </section>

      <!-- 列表 -->
      <template v-else>
        <p v-if="opError" class="ed__error" role="alert">{{ opError }}</p>
        <div class="sv__cards">
          <article
            v-for="s in list"
            :key="s.id"
            class="sop"
            :title="'查看 ' + s.name"
            @click="openDetail(s.id)"
          >
            <div class="sop__spine" />
            <div class="sop__body">
              <div class="sop__head">
                <h3 class="sop__name">{{ s.name }}</h3>
                <code class="sop__id">{{ s.id }}</code>
              </div>
              <p v-if="s.description" class="sop__desc">{{ s.description }}</p>
              <div v-if="s.triggers.length" class="sop__tags">
                <span v-for="t in s.triggers" :key="t" class="chip--static">{{ t }}</span>
              </div>
              <div class="sop__foot">
                <span class="sop__meta">
                  <template v-if="formatWhen(s.updated_at)">更新于 {{ formatWhen(s.updated_at) }}</template>
                  <template v-else>{{ s.key }}</template>
                </span>
                <!-- click.stop:卡片本身可点开详情,操作按钮不冒泡触发它 -->
                <div v-if="deleting === s.id" class="sop__act">
                  <span class="sop__meta">删除中…</span>
                </div>
                <div v-else-if="confirmId === s.id" class="sop__act sop__confirm" @click.stop>
                  <span class="sop__ask">删除「{{ s.name }}」?</span>
                  <button class="link link--del" type="button" @click="onDelete(s.id)">删除</button>
                  <button class="link" type="button" @click="confirmId = null">取消</button>
                </div>
                <div v-else class="sop__act" @click.stop>
                  <button class="link" type="button" @click="openDetail(s.id)">查看</button>
                  <button class="link" type="button" @click="openEditor(s.id)">编辑</button>
                  <button class="link link--del" type="button" @click="confirmId = s.id">删除</button>
                </div>
              </div>
            </div>
          </article>
        </div>
      </template>
    </div>
  </main>
</template>
