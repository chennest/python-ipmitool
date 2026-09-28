<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../api'
import type { FanInfo } from '../types'
import { fmtInt } from '../utils'

defineProps<{
  fans: FanInfo[]
  mode: 'auto' | 'manual'
}>()

const emit = defineEmits<{ (e: 'refresh'): void }>()

const pending = ref<string | null>(null)
const message = ref<string | null>(null)
/** 拖动中的临时值 —— 只在松手（change）时提交，避免拖动过程狂发请求 */
const draft = ref<Record<string, number>>({})

function onSlide(slot: string, ev: Event) {
  draft.value[slot] = Number((ev.target as HTMLInputElement).value)
}

async function commitDuty(slot: string) {
  const value = draft.value[slot]
  if (value === undefined) return
  pending.value = slot
  message.value = null
  try {
    // 拖到 0 = 把这一位交回 BMC 自动（EPYCD8 上 0x00 就是这个语义，
    // 接口层统一用 null 表达，所以这里做一次映射）
    await api.setManualDuty(slot, value === 0 ? null : value)
    message.value =
      value === 0 ? `${slot} 已交回 BMC 自动控制` : `${slot} 已设为 ${value}%`
    emit('refresh')
  } catch (e) {
    message.value = e instanceof Error ? e.message : String(e)
  } finally {
    pending.value = null
    delete draft.value[slot]
  }
}

/** 界面上该显示的值：拖动中用草稿值，否则用实际下发值（null 视为 0=自动） */
function displayed(fan: FanInfo): number {
  return draft.value[fan.slot] ?? fan.duty ?? 0
}

function dutyLabel(fan: FanInfo): string {
  if (draft.value[fan.slot] !== undefined) {
    const v = draft.value[fan.slot]
    return v === 0 ? '自动' : `${v}%`
  }
  return fan.duty === null || fan.duty === 0 ? '自动' : `${fan.duty}%`
}
</script>

<template>
  <section class="rounded-xl border border-zinc-200 bg-white">
    <!-- 卡片头 -->
    <div class="flex flex-wrap items-baseline gap-x-3 border-b border-zinc-100 px-4 py-3">
      <h2 class="text-[13px] font-semibold text-zinc-900">风扇</h2>
      <p class="text-[11px] text-zinc-400">
        {{
          mode === 'manual'
            ? '手动模式 —— 拖动滑块调速，拖到 0 交回 BMC'
            : '自动模式 —— 占空比由分配到的温度源的曲线决定'
        }}
      </p>
      <p v-if="message" class="ml-auto text-[11px] font-medium text-emerald-600">
        {{ message }}
      </p>
    </div>

    <!-- 表头 -->
    <div
      class="hidden items-center gap-4 border-b border-zinc-50 px-4 py-2 text-[11px] font-medium uppercase tracking-wide text-zinc-400 sm:flex"
    >
      <span class="w-28">风扇位</span>
      <span class="w-20 text-right">转速</span>
      <span class="flex-1">手动占空比</span>
      <span class="w-14 text-right">当前</span>
    </div>

    <ul class="divide-y divide-zinc-50">
      <li
        v-for="fan in fans"
        :key="fan.slot"
        class="px-4 py-3 transition hover:bg-zinc-50/60"
      >
        <div class="flex flex-wrap items-center gap-x-4 gap-y-2">
          <span class="w-28 text-xs font-medium text-zinc-900 tnum">
            {{ fan.slot }}
          </span>
          <span class="w-20 text-right text-xs text-zinc-600 tnum">
            {{ fmtInt(fan.rpm, ' RPM') }}
          </span>

          <input
            type="range"
            min="0"
            max="100"
            step="5"
            class="h-1.5 min-w-32 flex-1 cursor-pointer accent-zinc-900 disabled:cursor-not-allowed disabled:opacity-40"
            :value="displayed(fan)"
            :disabled="mode !== 'manual' || pending === fan.slot"
            :aria-label="`${fan.slot} 占空比`"
            @input="onSlide(fan.slot, $event)"
            @change="commitDuty(fan.slot)"
          />

          <span
            class="w-14 rounded-md px-1.5 py-0.5 text-center text-xs font-medium tnum"
            :class="
              fan.duty === null || fan.duty === 0
                ? 'bg-zinc-50 text-zinc-400'
                : 'bg-zinc-900 text-white'
            "
          >
            {{ dutyLabel(fan) }}
          </span>
        </div>

        <!-- 归属行：这个风扇位被哪个源占着（编辑入口在上方分配面板） -->
        <div class="mt-1.5 flex flex-wrap items-center gap-2 pl-0.5 text-[11px]">
          <span class="text-zinc-400">散热源</span>
          <span
            v-if="fan.owner_key"
            class="rounded-full bg-blue-50 px-2 py-0.5 font-medium text-blue-700 ring-1 ring-inset ring-blue-600/10"
          >
            {{ fan.bound_detail || fan.owner_key }}
          </span>
          <span
            v-else
            class="rounded-full bg-zinc-50 px-2 py-0.5 text-zinc-400 ring-1 ring-inset ring-zinc-200"
          >
            未分配 —— 由 BMC 自动
          </span>
          <span v-if="fan.temperature !== null" class="text-zinc-400 tnum">
            {{ fan.temperature.toFixed(1) }}°C
          </span>
        </div>
      </li>

      <li
        v-if="!fans.length"
        class="px-4 py-6 text-center text-xs text-zinc-400"
      >
        读不到风扇位 —— 检查 ipmi_exporter 是否在跑
      </li>
    </ul>

    <p class="border-t border-zinc-100 px-4 py-2.5 text-[11px] text-zinc-400">
      「哪张卡用哪个风扇」在上方分配面板里设置（以 GPU 为主体挑风扇接口）
    </p>
  </section>
</template>
