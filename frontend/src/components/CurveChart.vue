<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { CurveInfo, FanInfo } from '../types'
import { useChart } from '../composables/useChart'
import { CHART, chartBase } from '../theme'

const props = defineProps<{
  curve: CurveInfo | null
  fans: FanInfo[]
}>()

const el = ref<HTMLElement | null>(null)
const chart = useChart(el)

const ACCENT = '#18181b' // zinc-900 —— 工作点与曲线都用主色，克制

const option = computed(() => {
  const c = props.curve
  if (!c || !c.points.length) return {}

  // 阶梯用 ECharts 的 step: 'end' 直接画 —— 语义和控制器完全一致：
  // 温度必须**达到**折点才升档（见 app/curve.py 的说明）
  const stairs = c.points.map((p) => [p.temp, p.duty] as [number, number])

  // 当前工作点：取最热的那个受控风扇位所对应的温度/占空比
  const active = props.fans
    .filter((f) => f.temperature !== null)
    .sort((a, b) => (b.temperature ?? 0) - (a.temperature ?? 0))[0]

  const marks =
    active && active.duty !== null
      ? [
          {
            coord: [active.temperature as number, active.duty],
            symbolSize: 9,
            itemStyle: { color: ACCENT, borderColor: '#fff', borderWidth: 2 },
            label: {
              position: 'right' as const,
              fontSize: 11,
              fontWeight: 600 as const,
              color: ACCENT,
              formatter: () => `${active.temperature}°C → ${active.duty}%`,
            },
          },
        ]
      : []

  return {
    ...chartBase,
    grid: { left: 44, right: 70, top: 20, bottom: 28 },
    tooltip: {
      ...chartBase.tooltip,
      formatter: (params: unknown) => {
        const arr = params as { value: [number, number] }[]
        const v = arr[0]?.value
        return v ? `${v[0]}°C → ${v[1]}%` : ''
      },
    },
    xAxis: {
      type: 'value',
      name: '温度 °C',
      nameTextStyle: { fontSize: 11, color: CHART.textFaint },
      min: Math.max(0, (c.points[0]?.temp ?? 40) - 15),
      max: (c.points[c.points.length - 1]?.temp ?? 90) + 5,
      axisLabel: chartBase.axisLabel,
      splitLine: chartBase.splitLine,
    },
    yAxis: {
      type: 'value',
      name: '占空比 %',
      nameTextStyle: { fontSize: 11, color: CHART.textFaint },
      min: 0,
      max: 100,
      axisLabel: chartBase.axisLabel,
      splitLine: chartBase.splitLine,
    },
    series: [
      {
        type: 'line',
        step: 'end',
        data: stairs,
        symbol: 'circle',
        symbolSize: 5,
        lineStyle: { width: 1.5, color: ACCENT },
        itemStyle: { color: ACCENT },
        areaStyle: { color: 'rgba(24,24,27,0.04)' },
        markPoint: { data: marks, animation: false },
      },
    ],
  }
})

watch(option, (o) => chart.value?.setOption(o, true))
watch(chart, (c) => c && c.setOption(option.value, true))
</script>

<template>
  <section class="rounded-xl border border-zinc-200 bg-white">
    <div class="border-b border-zinc-100 px-4 py-3">
      <h2 class="text-[13px] font-semibold text-zinc-900">控制曲线</h2>
      <p class="mt-0.5 text-[11px] text-zinc-400">
        温度 → 占空比。阶梯语义：温度<b>达到</b>折点才升档；降温要跌出滞回带
        <template v-if="curve">（{{ curve.hysteresis }}°C）</template> 才降档
      </p>
    </div>
    <div class="p-3">
      <div ref="el" class="h-56 w-full" />
    </div>
  </section>
</template>
