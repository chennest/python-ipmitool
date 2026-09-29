<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useChart } from '../composables/useChart'
import { CHART, chartBase } from '../theme'
import type { HistorySeriesItem } from '../types'

/**
 * 单张趋势图（**纯展示**，数据由 TrendPanel 取好传进来）。
 *
 * 2026-09-28 从「温度+转速挤在一张双轴图」拆出来 —— 双轴图两个量纲差两个
 * 数量级，转速线被压成一条平的，谁跟谁联动根本看不出来。现在两条独立的图
 * 上下对照，图形形状直接可比。
 */
const props = defineProps<{
  series: HistorySeriesItem[]
  /** Y 轴单位（°C / RPM） */
  unit: string
  /** 系列配色 —— 温度暖色、转速冷色，语义两图分家 */
  palette: readonly string[]
  /** 高度类，默认 h-60 */
  height?: string
}>()

const el = ref<HTMLElement | null>(null)
const chart = useChart(el)

const option = computed(() => {
  const data = props.series
  if (!data.length) return {}

  const color = (i: number) => props.palette[i % props.palette.length]

  return {
    ...chartBase,
    grid: { left: 46, right: 14, top: 28, bottom: 22 },
    legend: chartBase.legend,
    tooltip: chartBase.tooltip,
    xAxis: {
      type: 'time',
      axisLabel: { ...chartBase.axisLabel, hideOverlap: true },
      axisLine: { lineStyle: { color: CHART.axis } },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      name: props.unit,
      nameTextStyle: { fontSize: 11, color: CHART.textFaint },
      // 不从 0 起 —— 温度常年 45~60°C，从 0 起会被压成一条直线，白白浪费高度
      scale: true,
      axisLabel: chartBase.axisLabel,
      splitLine: chartBase.splitLine,
    },
    series: data.map((s, i) => ({
      name: s.label,
      type: 'line' as const,
      showSymbol: false,
      sampling: 'lttb' as const, // 数据点多时降采样，曲线形状保留得不错
      lineStyle: { width: 1.5, color: color(i) },
      itemStyle: { color: color(i) },
      emphasis: { focus: 'series' as const },
      data: s.points,
    })),
  }
})

watch(option, (o) => chart.value?.setOption(o, true))
watch(chart, (c) => c && c.setOption(option.value, true))
</script>

<template>
  <div class="relative">
    <div ref="el" :class="height ?? 'h-60'" class="w-full" />

    <p
      v-if="!series.length"
      class="absolute inset-0 flex items-center justify-center text-xs text-zinc-400"
    >
      这一路暂无{{ unit === 'RPM' ? '转速' : '温度' }}样本
    </p>
  </div>
</template>
