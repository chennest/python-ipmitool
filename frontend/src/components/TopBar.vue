<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api'
import type { StatusSnapshot } from '../types'
import { fmtAgo } from '../utils'

const props = defineProps<{
  title: string
  subtitle: string
  snapshot: StatusSnapshot | null
}>()

const emit = defineEmits<{ (e: 'refresh'): void }>()

const switching = ref(false)
const restoring = ref(false)
const message = ref<string | null>(null)

const running = computed(() => props.snapshot?.running ?? false)
const emergency = computed(() => props.snapshot?.emergency ?? false)
const mode = computed(() => props.snapshot?.mode ?? null)

async function toggleMode() {
  if (!props.snapshot || switching.value) return
  const next = props.snapshot.mode === 'auto' ? 'manual' : 'auto'
  switching.value = true
  message.value = null
  try {
    await api.setMode(next)
    message.value = next === 'auto' ? '已切回自动模式' : '已切换为手动模式'
    emit('refresh')
  } catch (e) {
    message.value = e instanceof Error ? e.message : String(e)
  } finally {
    switching.value = false
  }
}

async function emergencyRestore() {
  if (restoring.value) return
  restoring.value = true
  message.value = null
  try {
    const r = await api.restoreAuto()
    message.value = r.ok
      ? '已把全部风扇位交回 BMC 自动控制'
      : '回落命令返回非零，请手动确认风扇状态'
    emit('refresh')
  } catch (e) {
    message.value = e instanceof Error ? e.message : String(e)
  } finally {
    restoring.value = false
  }
}
</script>

<template>
  <header
    class="sticky top-0 z-10 border-b border-zinc-200 bg-white/75 backdrop-blur-md"
  >
    <div class="flex flex-wrap items-center gap-3 px-4 py-3 md:px-6">
      <!-- 页面上下文 -->
      <div class="mr-auto leading-tight">
        <h1 class="text-sm font-semibold text-zinc-900">{{ title }}</h1>
        <p class="mt-0.5 text-[11px] text-zinc-400 tnum">
          {{ subtitle }}
          <template v-if="snapshot">
            · 控制周期 {{ snapshot.interval }}s · 上次执行
            {{ fmtAgo(snapshot.last_tick_ts) }}
          </template>
        </p>
      </div>

      <!-- 控制回路状态徽章 -->
      <span
        class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium ring-1 ring-inset"
        :class="
          emergency
            ? 'bg-red-50 text-red-700 ring-red-600/10'
            : running
              ? 'bg-emerald-50 text-emerald-700 ring-emerald-600/10'
              : 'bg-zinc-50 text-zinc-500 ring-zinc-500/10'
        "
      >
        <span
          class="size-1.5 rounded-full"
          :class="
            emergency
              ? 'animate-pulse bg-red-500'
              : running
                ? 'bg-emerald-500'
                : 'bg-zinc-400'
          "
        />
        {{ emergency ? '紧急散热' : running ? '运行中' : '已停止' }}
      </span>

      <!-- 模式切换：幽灵按钮 -->
      <button
        class="inline-flex h-8 items-center rounded-lg border border-zinc-200 bg-white px-3 text-xs font-medium text-zinc-700 shadow-xs transition hover:border-zinc-300 hover:bg-zinc-50 disabled:opacity-50"
        :disabled="switching || !snapshot"
        :title="
          mode === 'auto'
            ? '当前按曲线自动调档，点击改为手动'
            : '当前手动固定占空比，点击改回自动'
        "
        @click="toggleMode"
      >
        模式 · {{ mode === 'auto' ? '自动' : mode === 'manual' ? '手动' : '—' }}
      </button>

      <!-- 紧急回落：安全方向操作，任何时候都该点得动 -->
      <button
        class="inline-flex h-8 items-center rounded-lg bg-zinc-900 px-3 text-xs font-medium text-white shadow-xs transition hover:bg-zinc-800 disabled:opacity-50"
        :disabled="restoring"
        title="把所有风扇位交回 BMC 自动控制"
        @click="emergencyRestore"
      >
        {{ restoring ? '处理中…' : '交回 BMC' }}
      </button>
    </div>

    <!-- 错误 / 操作反馈：alert 条 -->
    <div
      v-if="message || snapshot?.last_error"
      class="px-4 pb-2.5 md:px-6"
    >
      <p
        class="rounded-lg px-3 py-2 text-xs font-medium"
        :class="
          snapshot?.last_error
            ? 'bg-red-50 text-red-700 ring-1 ring-inset ring-red-600/10'
            : 'bg-zinc-50 text-zinc-600 ring-1 ring-inset ring-zinc-500/10'
        "
      >
        {{ snapshot?.last_error ?? message }}
      </p>
    </div>
  </header>
</template>
