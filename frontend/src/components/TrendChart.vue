<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import { useChart } from '../composables/useChart'
import { CHART, chartBase } from '../theme'
import type { HistoryResponse } from '../types'

const el = ref<HTMLElement | null>(null)
const chart = useChart(el)

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

const option = computed(() => {
  const d = data.value
  if (!d || !d.series.length) return {}

  return {
    ...chartBase,
    grid: { ...chartBase.grid, top: 34 },
    legend: chartBase.legend,
    tooltip: chartBase.tooltip,
    xAxis: {
      type: 'time',
      axisLabel: { ...chartBase.axisLabel, hideOverlap: true },
      axisLine: { lineStyle: { color: CHART.axis } },
      splitLine: { show: false },
    },
    // 温度和转速量纲差两个数量级，必须分轴，否则转速线会被压成一条平的
    yAxis: [
      {
        type: 'value',
        name: '°C',
        nameTextStyle: { fontSize: 11, color: CHART.textFaint },
        axisLabel: chartBase.axisLabel,
        splitLine: chartBase.splitLine,
      },
      {
        type: 'value',
        name: 'RPM',
        nameTextStyle: { fontSize: 11, color: CHART.textFaint },
        axisLabel: chartBase.axisLabel,
        splitLine: { show: false },
      },
    ],
    series: d.series.map((s, i) => ({
      name: s.label,
      type: 'line' as const,
      yAxisIndex: s.axis === 'right' ? 1 : 0,
      showSymbol: false,
      sampling: 'lttb' as const, // 数据点多时降采样，曲线形状保留得不错
      lineStyle: { width: 1.5, color: CHART.series[i % CHART.series.length] },
      itemStyle: { color: CHART.series[i % CHART.series.length] },
      emphasis: { focus: 'series' as const },
      data: s.points,
    })),
  }
})

watch(option, (o) => chart.value?.setOption(o, true))
watch(chart, (c) => c && c.setOption(option.value, true))
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
    <!-- 卡片头 + 分段控件 -->
    <div class="flex flex-wrap items-center gap-3 border-b border-zinc-100 px-4 py-3">
      <div class="mr-auto">
        <h2 class="text-[13px] font-semibold text-zinc-900">温度 / 转速趋势</h2>
        <p class="mt-0.5 text-[11px] text-zinc-400">数据来自 Prometheus，30s 粒度</p>
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

    <div class="p-3">
      <div ref="el" class="h-64 w-full" />

      <p
        v-if="!loading && data && !data.series.length"
        class="py-10 text-center text-xs text-zinc-400"
      >
        暂无历史数据 —— 时间范围内没有样本
      </p>
    </div>
  </section>
</template>
