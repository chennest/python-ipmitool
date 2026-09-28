<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import TrendChart from './TrendChart.vue'
import { CHART } from '../theme'
import type { HistoryResponse } from '../types'

/**
 * 趋势区 —— **温度、转速两张独立的图**（2026-09-28 超哥要求拆开）。
 *
 * 为什么拆：原来一张双 Y 轴图，温度和转速差两个数量级，转速线被压成一条平线，
 * 想看「温度涨了风扇跟没跟上」全靠猜。分开后两张图形状直接可比。
 *
 * 为什么数据只取一次、范围控件只有一个：两张图必须看**同一个时间窗口**，
 * 各自一个范围选择器迟早出现「上面 15 分钟、下面 3 小时」的错位对照。
 */
const data = ref<HistoryResponse | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)

const RANGES = [
  { label: '15 分钟', minutes: 15 },
  { label: '30 分钟', minutes: 30 },
  { label: '1 小时', minutes: 60 },
  { label: '3 小时', minutes: 180 },
]
const minutes = ref(30)

async function load() {
  loading.value = true
  error.value = null
  try {
    data.value = await api.history(minutes.value)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

/** 按单位分组 —— 后端给每个系列都带了 unit（°C / RPM），不用回来猜 */
const tempSeries = computed(
  () => data.value?.series.filter((s) => s.unit !== 'RPM') ?? [],
)
const rpmSeries = computed(() => data.value?.series.filter((s) => s.unit === 'RPM') ?? [])

const subtitle = computed(() => {
  if (data.value?.note) return data.value.note
  return '数据来自 Prometheus，30s 粒度'
})

watch(minutes, load)

let timer: number | null = null
onMounted(() => {
  void load()
  // Prometheus 那边 30s 才 scrape 一次，前端 60s 拉一次足够，没必要更密
  timer = window.setInterval(() => void load(), 60_000)
})
onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer)
})
</script>

<template>
  <section class="rounded-xl border border-zinc-200 bg-white">
    <!-- 卡片头 + 分段控件（两张图共用一个时间范围） -->
    <div class="flex flex-wrap items-center gap-3 border-b border-zinc-100 px-4 py-3">
      <div class="mr-auto">
        <h2 class="text-[13px] font-semibold text-zinc-900">趋势</h2>
        <p class="mt-0.5 text-[11px] text-zinc-400">{{ subtitle }}</p>
      </div>

      <p v-if="loading" class="text-[11px] text-zinc-400">加载中…</p>
      <p v-else-if="error" class="text-[11px] font-medium text-red-600">{{ error }}</p>

      <!-- segmented control -->
      <div class="flex rounded-lg bg-zinc-100 p-0.5">
        <button
          v-for="r in RANGES"
          :key="r.minutes"
          class="rounded-md px-2.5 py-1 text-xs font-medium transition"
          :class="
            minutes === r.minutes
              ? 'bg-white text-zinc-900 shadow-sm'
              : 'text-zinc-500 hover:text-zinc-900'
          "
          @click="minutes = r.minutes"
        >
          {{ r.label }}
        </button>
      </div>
    </div>

    <div class="grid gap-3 p-3 xl:grid-cols-2">
      <!-- 温度 -->
      <div class="rounded-lg border border-zinc-100 p-2">
        <div class="flex items-baseline gap-2 px-1 pb-1">
          <h3 class="text-xs font-semibold text-zinc-900">温度</h3>
          <span class="text-[11px] text-zinc-400">GPU 核心温度 + CPU Tctl</span>
          <span class="ml-auto text-[11px] text-zinc-400 tnum">
            {{ tempSeries.length }} 路
          </span>
        </div>
        <TrendChart :series="tempSeries" unit="°C" :palette="CHART.tempSeries" />
      </div>

      <!-- 转速 -->
      <div class="rounded-lg border border-zinc-100 p-2">
        <div class="flex items-baseline gap-2 px-1 pb-1">
          <h3 class="text-xs font-semibold text-zinc-900">转速</h3>
          <span class="text-[11px] text-zinc-400">
            风扇实测 RPM —— 受控位会跟着左边温度走
          </span>
          <span class="ml-auto text-[11px] text-zinc-400 tnum">
            {{ rpmSeries.length }} 路
          </span>
        </div>
        <TrendChart :series="rpmSeries" unit="RPM" :palette="CHART.rpmSeries" />
      </div>
    </div>
  </section>
</template>
