<script setup lang="ts">
import { computed } from 'vue'
import type { TemperatureInfo } from '../types'
import { fmt, tempBarColor, tempColor } from '../utils'

const props = defineProps<{ temperatures: TemperatureInfo | null }>()

/** Tctl 是 AMD 的「控制温度」，也是 CPU 温度里最该盯的一个 */
const tctl = computed(
  () => props.temperatures?.cpu_cores.find((t) => t.label === 'Tctl') ?? null,
)

/** 其余核心温度（每个 CCD 一路 Tccd） */
const otherCores = computed(
  () => (props.temperatures?.cpu_cores ?? []).filter((t) => t.label !== 'Tctl'),
)

const board = computed(() => props.temperatures?.board ?? [])

const hasData = computed(() => tctl.value !== null || board.value.length > 0)
</script>

<template>
  <section class="rounded-xl border border-zinc-200 bg-white">
    <!-- 卡片头 -->
    <div class="flex flex-wrap items-baseline gap-x-3 border-b border-zinc-100 px-4 py-3">
      <h2 class="text-[13px] font-semibold text-zinc-900">温度</h2>
      <p class="text-[11px] text-zinc-400">
        CPU 以 node_exporter 的 Tctl 为准；板载温度仅作参照（不含 GPU）
      </p>
    </div>

    <p
      v-if="!hasData"
      class="m-4 rounded-lg bg-amber-50 px-3 py-2 text-xs font-medium text-amber-800 ring-1 ring-inset ring-amber-600/10"
    >
      读不到温度 —— 检查 node_exporter 的 hwmon 采集与 ipmi_exporter 是否在跑
    </p>

    <div v-else class="grid gap-x-6 gap-y-4 p-4 sm:grid-cols-2">
      <!-- CPU 核心 -->
      <div>
        <div class="flex items-end justify-between">
          <div>
            <p class="text-[11px] font-medium uppercase tracking-wide text-zinc-400">
              CPU 核心温度
            </p>
            <p v-if="tctl" class="mt-1 text-3xl font-semibold tracking-tight tnum" :class="tempColor(tctl.celsius)">
              {{ fmt(tctl.celsius, 1) }}<span class="ml-0.5 text-sm font-medium">°C</span>
            </p>
          </div>
          <span v-if="tctl" class="pb-1 text-[11px] text-zinc-400">Tctl</span>
        </div>

        <div v-if="tctl" class="mt-2.5 h-1 w-full overflow-hidden rounded-full bg-zinc-100">
          <div
            class="h-full rounded-full transition-all"
            :class="tempBarColor(tctl.celsius)"
            :style="{ width: `${Math.min(100, Math.max(0, tctl.celsius ?? 0))}%` }"
          />
        </div>

        <dl v-if="otherCores.length" class="mt-3 space-y-1 text-xs">
          <div
            v-for="core in otherCores"
            :key="core.label"
            class="flex items-center justify-between border-b border-zinc-50 pb-1 last:border-0"
          >
            <dt class="text-zinc-400">{{ core.label }}</dt>
            <dd class="font-medium tnum" :class="tempColor(core.celsius)">
              {{ fmt(core.celsius, 1, '°C') }}
            </dd>
          </div>
        </dl>
      </div>

      <!-- BMC 板载 -->
      <div class="border-t border-zinc-100 pt-4 sm:border-l sm:border-t-0 sm:pl-6 sm:pt-0">
        <p class="text-[11px] font-medium uppercase tracking-wide text-zinc-400">
          板载环境温度
          <span class="ml-1 normal-case text-zinc-300">BMC</span>
        </p>
        <dl class="mt-2.5 grid grid-cols-1 gap-y-1 text-xs sm:max-h-44 sm:overflow-y-auto">
          <div
            v-for="t in board"
            :key="t.name"
            class="flex items-center justify-between border-b border-zinc-50 pb-1 last:border-0"
          >
            <dt class="truncate text-zinc-500" :title="t.name">{{ t.name }}</dt>
            <dd class="font-medium tnum" :class="tempColor(t.celsius)">
              {{ fmt(t.celsius, 0, '°C') }}
            </dd>
          </div>
        </dl>
      </div>
    </div>
  </section>
</template>
