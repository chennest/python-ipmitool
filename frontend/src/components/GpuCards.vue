<script setup lang="ts">
import type { AssignmentItem, GpuInfo } from '../types'
import { fmt, fmtInt, tempBarColor, tempColor } from '../utils'

const props = defineProps<{
  gpus: GpuInfo[]
  /** 全部分配（用来在每张卡上显示它管着哪些风扇位） */
  assignments: AssignmentItem[]
}>()

/** 这张卡分到的风扇位 */
function slotsOf(gpu: GpuInfo): string[] {
  return (
    props.assignments.find((a) => a.kind === 'gpu' && a.gpu_uuid === gpu.uuid)
      ?.slots ?? []
  )
}
</script>

<template>
  <section class="grid gap-4 sm:grid-cols-2">
    <article
      v-for="gpu in gpus"
      :key="gpu.uuid"
      class="rounded-xl border border-zinc-200 bg-white"
    >
      <div class="p-4">
        <!-- 标签行 -->
        <div class="flex items-center justify-between">
          <!-- 显示短 uuid 而不是 index：这台机器的两张卡换过 PCI 槽位，
               index 和 pci_bus_id 都变过，只有 uuid 稳定 -->
          <p class="text-xs font-medium text-zinc-500 tnum">
            GPU {{ gpu.index }}
            <span class="ml-1.5 text-zinc-900">{{ gpu.short_uuid }}</span>
          </p>
          <span class="text-[11px] text-zinc-400">{{ gpu.model }}</span>
        </div>

        <!-- 主读数 -->
        <div class="mt-3 flex items-end justify-between gap-3">
          <span class="text-4xl font-semibold tracking-tight tnum" :class="tempColor(gpu.temperature)">
            {{ fmtInt(gpu.temperature) }}<span class="ml-0.5 text-lg font-medium">°C</span>
          </span>
          <div class="pb-1 text-right text-[11px] leading-relaxed text-zinc-500 tnum">
            <p>{{ fmt(gpu.power_watts, 1, ' W') }}</p>
            <p>{{ fmtInt(gpu.utilization, '%') }} 利用率</p>
            <p>{{ fmtInt(gpu.clock_mhz, ' MHz') }} 频率</p>
          </div>
        </div>

        <!-- 温度条：按 0~100°C 映射，>70 转琥珀、>80 转红 -->
        <div class="mt-3 h-1 w-full overflow-hidden rounded-full bg-zinc-100">
          <div
            class="h-full rounded-full transition-all"
            :class="tempBarColor(gpu.temperature)"
            :style="{ width: `${Math.min(100, Math.max(0, gpu.temperature ?? 0))}%` }"
          />
        </div>

        <!-- 细项 -->
        <dl class="mt-3 grid grid-cols-2 gap-x-6 border-t border-zinc-100 pt-3 text-xs">
          <div class="flex items-center justify-between">
            <dt class="text-zinc-400">显存</dt>
            <dd class="text-zinc-700 tnum">
              <template v-if="gpu.memory_total_mib">
                {{ fmtInt(gpu.memory_used_mib) }} /
                {{ fmtInt(gpu.memory_total_mib) }} MiB
              </template>
              <template v-else>—</template>
            </dd>
          </div>
          <div class="flex items-center justify-between">
            <dt class="text-zinc-400">占用</dt>
            <dd class="text-zinc-700 tnum">{{ fmt(gpu.memory_percent, 0, '%') }}</dd>
          </div>
        </dl>
      </div>

      <!-- 这张卡分到的风扇位（GPU 是主体 —— 这里是「结果」的展示，
           编辑入口在「风扇控制」页） -->
      <div class="border-t border-zinc-100 px-4 py-2.5">
        <p class="flex flex-wrap items-center gap-1.5 text-xs">
          <span class="text-zinc-400">风扇</span>
          <template v-if="slotsOf(gpu).length">
            <span
              v-for="slot in slotsOf(gpu)"
              :key="slot"
              class="rounded-full bg-zinc-900 px-2 py-0.5 text-[11px] font-medium tnum text-white"
            >
              {{ slot }}
            </span>
          </template>
          <span
            v-else
            class="rounded-full bg-zinc-50 px-2 py-0.5 text-[11px] text-zinc-400 ring-1 ring-inset ring-zinc-200"
          >
            未分配
          </span>
        </p>
      </div>
    </article>

    <p
      v-if="!gpus.length"
      class="rounded-xl border border-dashed border-zinc-300 bg-white p-6 text-center text-xs text-zinc-400 sm:col-span-2"
    >
      读不到 GPU 指标 —— 检查 DCGM exporter 是否在跑
    </p>
  </section>
</template>
