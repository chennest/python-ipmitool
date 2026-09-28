<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api'
import type { CurvePoint, GpuInfo } from '../types'
import { useChart } from '../composables/useChart'
import { CHART, chartBase } from '../theme'

/**
 * 温度 → 占空比曲线编辑器。
 *
 * 设计要点（2026-09-28 重做）：
 * 1. **能看见** —— 以前只有一排光秃秃的数字，改完不知道自己画成啥样了。
 *    现在左边改数字、右边阶梯图实时跟着动。
 * 2. **不自己发明算法** —— 预览图的数据来自后端 ``POST /api/curve/preview``，
 *    用的是控制器真正会跑的那套（阶梯语义 + 上下限钳制）。前端照着公式再实现
 *    一遍必然会和后端漂移，迟早出现「图上 70%、实际 65%」的鬼故事。
 *    后端不可达时才用本地兜底算法（同时把错误显示出来，不假装没事）。
 * 3. **当场校验** —— 温度重复 / 占空比越界 / 上下限倒置，在保存按钮之前就红出来。
 */

const points = defineModel<CurvePoint[]>('points', { required: true })
const hysteresis = defineModel<number>('hysteresis', { required: true })
const minDuty = defineModel<number>('minDuty', { required: true })
const maxDuty = defineModel<number>('maxDuty', { required: true })

const props = defineProps<{
  /** 实时 GPU 列表 —— 用来在图上标出「我现在在哪一档」 */
  gpus: GpuInfo[]
  /** 紧急拉满阈值（画一条红线做参照，可选） */
  emergencyTemp?: number | null
}>()

const emit = defineEmits<{ change: []; validity: [boolean] }>()

// ------------------------------------------------------------------ 校验

interface Issue {
  level: 'error' | 'warn'
  /** 定位到第几行折点；null = 属于全局参数（滞回带 / 上下限） */
  row: number | null
  msg: string
}

const issues = computed<Issue[]>(() => {
  const out: Issue[] = []
  const pts = points.value

  const seen = new Map<number, number>()
  pts.forEach((p, i) => {
    if (!Number.isFinite(p.temp)) {
      out.push({ level: 'error', row: i, msg: '温度必须是数字' })
    } else if (p.temp < 0 || p.temp > 125) {
      out.push({ level: 'error', row: i, msg: '温度需在 0~125°C 之间' })
    }
    if (!Number.isFinite(p.duty)) {
      out.push({ level: 'error', row: i, msg: '占空比必须是数字' })
    } else if (!Number.isInteger(p.duty)) {
      out.push({ level: 'error', row: i, msg: '占空比必须是整数百分比' })
    } else if (p.duty < 1 || p.duty > 100) {
      out.push({ level: 'error', row: i, msg: '占空比需在 1~100% 之间' })
    }

    if (Number.isFinite(p.temp)) {
      if (seen.has(p.temp)) {
        out.push({
          level: 'error',
          row: i,
          msg: `与第 ${(seen.get(p.temp) ?? 0) + 1} 行温度重复（后端会拒绝）`,
        })
      } else {
        seen.set(p.temp, i)
      }
    }
  })

  // 温度没按递增排 —— 后端会自己排序，不是错误，但显示顺序会误导人
  for (let i = 1; i < pts.length; i++) {
    if (Number.isFinite(pts[i].temp) && Number.isFinite(pts[i - 1].temp) && pts[i].temp < pts[i - 1].temp) {
      out.push({ level: 'warn', row: i, msg: '折点未按温度递增，建议点「按温度排序」' })
      break
    }
  }

  const mn = minDuty.value
  const mx = maxDuty.value
  if (!Number.isInteger(mn) || mn < 1 || mn > 100) {
    out.push({ level: 'error', row: null, msg: '占空比下限需是 1~100 的整数' })
  }
  if (!Number.isInteger(mx) || mx < 1 || mx > 100) {
    out.push({ level: 'error', row: null, msg: '占空比上限需是 1~100 的整数' })
  }
  if (Number.isInteger(mn) && Number.isInteger(mx) && mn > mx) {
    out.push({
      level: 'error',
      row: null,
      msg: `下限（${mn}%）高于上限（${mx}%）—— 会被钳成常量，折点怎么改都不动`,
    })
  }
  if (!Number.isFinite(hysteresis.value) || hysteresis.value < 0 || hysteresis.value > 20) {
    out.push({ level: 'error', row: null, msg: '滞回带需在 0~20°C 之间' })
  }

  // 折点被上下限吃掉 —— 不算错，但要知道「填了 20% 实际是 35%」
  pts.forEach((p, i) => {
    if (!Number.isInteger(p.duty)) return
    if (Number.isInteger(mn) && p.duty < mn) {
      out.push({ level: 'warn', row: i, msg: `低于下限，实际会被抬到 ${mn}%` })
    }
    if (Number.isInteger(mx) && p.duty > mx) {
      out.push({ level: 'warn', row: i, msg: `高于上限，实际会被压到 ${mx}%` })
    }
  })

  return out
})

const errors = computed(() => issues.value.filter((i) => i.level === 'error'))

function rowIssues(i: number, level: 'error' | 'warn') {
  return issues.value.filter((x) => x.row === i && x.level === level).map((x) => x.msg)
}
const globalErrors = computed(() => errors.value.filter((i) => i.row === null).map((i) => i.msg))

watch(
  () => errors.value.length,
  (n) => emit('validity', n === 0),
  { immediate: true },
)

// ------------------------------------------------------------------ 预览数据

const samples = ref<{ temp: number; duty: number }[]>([])
const previewError = ref<string | null>(null)

/** 本地兜底：后端试算挂了也不能让图空着（逻辑与后端 duty_at 一致） */
function localDutyAt(temp: number): number {
  const pts = points.value
    .filter((p) => Number.isFinite(p.temp) && Number.isFinite(p.duty))
    .sort((a, b) => a.temp - b.temp)
  if (!pts.length) return 0
  let duty = pts[0].duty
  for (const p of pts) {
    if (temp >= p.temp) duty = p.duty
    else break
  }
  return Math.max(minDuty.value, Math.min(maxDuty.value, Math.round(duty)))
}

function localSamples(from: number, to: number) {
  const out: { temp: number; duty: number }[] = []
  for (let t = from; t <= to + 1e-9; t += 1) out.push({ temp: t, duty: localDutyAt(t) })
  return out
}

const range = computed(() => {
  const temps = points.value.map((p) => p.temp).filter((t) => Number.isFinite(t))
  const first = temps.length ? Math.min(...temps) : 40
  const last = temps.length ? Math.max(...temps) : 90
  const from = Math.max(0, Math.floor(first - 15))
  const to = Math.min(130, Math.ceil(Math.max(last + 10, props.emergencyTemp ?? 0) + 5))
  return { from, to: Math.max(to, from + 20) }
})

let timer: ReturnType<typeof setTimeout> | undefined
async function refresh() {
  const { from, to } = range.value
  try {
    const resp = await api.previewCurve({
      points: points.value,
      hysteresis: hysteresis.value,
      min_duty: minDuty.value,
      max_duty: maxDuty.value,
      from,
      to,
      step: 1,
    })
    samples.value = resp.samples
    previewError.value = null
  } catch (e) {
    samples.value = localSamples(from, to)
    previewError.value = e instanceof Error ? e.message : String(e)
  }
}

watch(
  [points, hysteresis, minDuty, maxDuty, () => range.value.from, () => range.value.to],
  () => {
    clearTimeout(timer)
    // 敲数字时别每个字符都发一次请求
    timer = setTimeout(refresh, 250)
  },
  { deep: true, immediate: true },
)

/** 按温度排序后的折点 —— 语义说明里要指「第一个折点」，得先排好序 */
const ordered = computed(() =>
  [...points.value].filter((p) => Number.isFinite(p.temp)).sort((a, b) => a.temp - b.temp),
)
const firstPoint = computed(() => ordered.value[0] ?? null)
/** 当前每张卡落在这条曲线的哪一档 —— 图上的工作点 + 文字清单都用它 */
const workPoints = computed(() => {
  const byTemp = new Map(samples.value.map((s) => [Math.round(s.temp), s.duty]))
  return props.gpus
    .filter((g) => g.temperature !== null)
    .map((g) => {
      const t = Math.round(g.temperature as number)
      return {
        uuid: g.short_uuid,
        temp: g.temperature as number,
        duty: byTemp.get(t) ?? localDutyAt(t),
      }
    })
    .sort((a, b) => b.temp - a.temp)
})

const activeBand = computed(() => {
  const h = workPoints.value[0]
  if (!h || !Number.isFinite(hysteresis.value) || hysteresis.value <= 0) return null
  // 找到这个温度所处的折点（阶梯语义：取最后一个 temp ≤ 当前温度的折点）
  const anchor = ordered.value.filter((p) => h.temp >= p.temp).pop()
  if (!anchor) return null
  return { from: anchor.temp - hysteresis.value, to: anchor.temp, duty: h.duty }
})

// ------------------------------------------------------------------ 图

const el = ref<HTMLElement | null>(null)
const chart = useChart(el)
const ACCENT = '#18181b'

const option = computed(() => {
  const data = samples.value.map((s) => [s.temp, s.duty] as [number, number])
  const emergency = props.emergencyTemp

  const markLines: Record<string, unknown>[] = []
  if (Number.isInteger(minDuty.value)) {
    markLines.push({
      yAxis: minDuty.value,
      lineStyle: { color: CHART.axis, type: 'dashed', width: 1 },
      label: {
        formatter: `下限 ${minDuty.value}%`,
        position: 'insideStartTop',
        fontSize: 10,
        color: CHART.textFaint,
      },
    })
  }
  if (Number.isInteger(maxDuty.value) && maxDuty.value < 100) {
    markLines.push({
      yAxis: maxDuty.value,
      lineStyle: { color: CHART.axis, type: 'dashed', width: 1 },
      label: {
        formatter: `上限 ${maxDuty.value}%`,
        position: 'insideStartTop',
        fontSize: 10,
        color: CHART.textFaint,
      },
    })
  }
  if (emergency && emergency <= range.value.to) {
    markLines.push({
      xAxis: emergency,
      lineStyle: { color: '#dc2626', type: 'dashed', width: 1 },
      label: {
        formatter: `紧急 ${emergency}°C`,
        position: 'insideEndTop',
        fontSize: 10,
        color: '#dc2626',
      },
    })
  }
  workPoints.value.forEach((w) => {
    markLines.push({
      xAxis: w.temp,
      lineStyle: { color: '#0ea5e9', type: 'dotted', width: 1 },
      label: {
        formatter: `${w.uuid} ${w.temp.toFixed(0)}°C`,
        position: 'start',
        fontSize: 10,
        color: '#0ea5e9',
      },
    })
  })

  const markAreas = activeBand.value
    ? [
        [
          {
            xAxis: activeBand.value.from,
            itemStyle: { color: 'rgba(14,165,233,0.07)' },
            label: {
              formatter: '滞回带',
              position: 'insideTop',
              fontSize: 10,
              color: CHART.textFaint,
            },
          },
          { xAxis: activeBand.value.to },
        ],
      ]
    : []

  return {
    ...chartBase,
    grid: { left: 46, right: 24, top: 18, bottom: 26 },
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
      min: range.value.from,
      max: range.value.to,
      axisLabel: chartBase.axisLabel,
      splitLine: chartBase.splitLine,
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLabel: chartBase.axisLabel,
      splitLine: chartBase.splitLine,
    },
    series: [
      {
        type: 'line',
        step: 'end',
        data,
        symbol: 'none',
        lineStyle: { width: 1.5, color: ACCENT },
        itemStyle: { color: ACCENT },
        areaStyle: { color: 'rgba(24,24,27,0.04)' },
        markLine: { silent: true, symbol: 'none', data: markLines },
        markArea: { silent: true, data: markAreas },
        markPoint: {
          data: workPoints.value.map((w) => ({
            coord: [w.temp, w.duty],
            symbolSize: 8,
            itemStyle: { color: '#0ea5e9', borderColor: '#fff', borderWidth: 2 },
          })),
        },
      },
    ],
  }
})

watch(option, (o) => chart.value?.setOption(o, true))
watch(chart, (c) => c && c.setOption(option.value, true))

// ------------------------------------------------------------------ 编辑动作

function touch() {
  emit('change')
}

function addPoint() {
  const pts = [...points.value]
  const last = pts.length ? pts[pts.length - 1] : { temp: 60, duty: 50 }
  pts.push({
    temp: Math.min(125, Math.round(last.temp) + 10),
    duty: Math.min(100, Math.round(last.duty) + 15),
  })
  points.value = pts
  touch()
}

function removePoint(i: number) {
  if (points.value.length <= 1) return
  points.value = points.value.filter((_, idx) => idx !== i)
  touch()
}

function sortByTemp() {
  points.value = [...points.value].sort((a, b) => a.temp - b.temp)
  touch()
}

/** 预设：只填草稿，要点保存才生效 */
const PRESETS: { name: string; desc: string; points: CurvePoint[]; hysteresis: number; minDuty: number }[] = [
  {
    name: '静音',
    desc: '低温段压得低，噪音小；靠紧急阈值兜底',
    points: [
      { temp: 45, duty: 30 },
      { temp: 60, duty: 45 },
      { temp: 70, duty: 60 },
      { temp: 80, duty: 80 },
      { temp: 88, duty: 100 },
    ],
    hysteresis: 4,
    minDuty: 25,
  },
  {
    name: '均衡',
    desc: '温度上来就提速，噪音与温度折中',
    points: [
      { temp: 40, duty: 40 },
      { temp: 55, duty: 55 },
      { temp: 68, duty: 72 },
      { temp: 78, duty: 88 },
      { temp: 86, duty: 100 },
    ],
    hysteresis: 3,
    minDuty: 30,
  },
  {
    name: '激进',
    desc: '宁可吵也要压温度，适合满载推理',
    points: [
      { temp: 35, duty: 50 },
      { temp: 50, duty: 65 },
      { temp: 65, duty: 80 },
      { temp: 75, duty: 100 },
    ],
    hysteresis: 2,
    minDuty: 45,
  },
]

function applyPreset(p: (typeof PRESETS)[number]) {
  points.value = p.points.map((x) => ({ ...x }))
  hysteresis.value = p.hysteresis
  minDuty.value = p.minDuty
  maxDuty.value = 100
  touch()
}

const presetActive = computed(() => {
  const cur = JSON.stringify(
    [...points.value].sort((a, b) => a.temp - b.temp),
  )
  return (p: (typeof PRESETS)[number]) =>
    JSON.stringify([...p.points].sort((a, b) => a.temp - b.temp)) === cur &&
    p.hysteresis === hysteresis.value &&
    p.minDuty === minDuty.value
})
</script>

<template>
  <div>
    <div class="mb-3 flex flex-wrap items-baseline gap-2">
      <h3 class="text-xs font-semibold text-zinc-900">温度 → 占空比曲线</h3>
      <span class="text-[11px] text-zinc-400">
        温度<b>达到</b>折点才升档；降温要跌出滞回带才降档
      </span>
    </div>

    <!-- 预览图 + 工作点 -->
    <div class="rounded-lg border border-zinc-200 bg-zinc-50/40 p-2">
      <div ref="el" class="h-52 w-full" />
      <div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 px-1 text-[11px]">
        <span v-if="workPoints.length" class="text-zinc-400">当前工作点</span>
        <span
          v-for="w in workPoints"
          :key="w.uuid"
          class="inline-flex items-center gap-1 rounded-full bg-white px-2 py-0.5 ring-1 ring-inset ring-zinc-200"
        >
          <span class="size-1.5 rounded-full bg-sky-500" />
          <span class="font-medium text-zinc-700 tnum">{{ w.uuid }}</span>
          <span class="text-zinc-400 tnum">{{ w.temp.toFixed(0) }}°C</span>
          <span class="font-medium text-zinc-900 tnum">{{ w.duty }}%</span>
        </span>
        <span v-if="!workPoints.length" class="text-zinc-400">
          还没有温度读数，图上只画曲线本身
        </span>
        <span v-if="previewError" class="ml-auto text-amber-600">
          试算接口不可用，已用本地算法兜底
        </span>
      </div>
    </div>

    <!-- 预设 -->
    <div class="mt-3 flex flex-wrap items-center gap-2">
      <span class="text-[11px] text-zinc-400">预设</span>
      <button
        v-for="p in PRESETS"
        :key="p.name"
        class="h-7 rounded-md border px-2.5 text-[11px] font-medium transition"
        :class="
          presetActive(p)
            ? 'border-zinc-900 bg-zinc-900 text-white'
            : 'border-zinc-200 bg-white text-zinc-600 hover:border-zinc-300 hover:text-zinc-900'
        "
        :title="p.desc"
        @click="applyPreset(p)"
      >
        {{ p.name }}
      </button>
      <span class="text-[11px] text-zinc-400">（只填进草稿，要点保存才生效）</span>
    </div>

    <!-- 折点表 -->
    <div class="mt-3 space-y-1.5">
      <div
        v-for="(p, i) in points"
        :key="i"
        class="rounded-lg border px-2.5 py-2"
        :class="rowIssues(i, 'error').length ? 'border-red-300 bg-red-50/40' : 'border-zinc-200'"
      >
        <div class="flex items-center gap-2 text-xs">
          <span class="w-5 text-center text-[11px] text-zinc-300">{{ i + 1 }}</span>
          <input
            v-model.number="p.temp"
            type="number"
            step="1"
            class="h-8 w-20 rounded-md border px-2 tnum focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
            :class="
              rowIssues(i, 'error').some((m) => m.includes('温度'))
                ? 'border-red-400'
                : 'border-zinc-300 focus:border-zinc-400'
            "
            @change="touch"
          />
          <span class="text-zinc-400">°C →</span>
          <input
            v-model.number="p.duty"
            type="number"
            step="1"
            min="1"
            max="100"
            class="h-8 w-20 rounded-md border px-2 tnum focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
            :class="
              rowIssues(i, 'error').some((m) => m.includes('占空比'))
                ? 'border-red-400'
                : 'border-zinc-300 focus:border-zinc-400'
            "
            @change="touch"
          />
          <span class="text-zinc-400">%</span>

          <!-- 这一档真正会下发的值（含上下限钳制）—— 填的和生效的不一样就露出来 -->
          <span
            v-if="Number.isInteger(p.duty) && localDutyAt(p.temp) !== p.duty"
            class="rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700 ring-1 ring-inset ring-amber-600/10"
          >
            实际 {{ localDutyAt(p.temp) }}%
          </span>

          <button
            class="ml-auto rounded-md px-2 py-1 text-[11px] font-medium text-zinc-400 transition hover:bg-red-50 hover:text-red-600 disabled:opacity-30"
            :disabled="points.length <= 1"
            @click="removePoint(i)"
          >
            删除
          </button>
        </div>

        <ul v-if="rowIssues(i, 'error').length || rowIssues(i, 'warn').length" class="mt-1 space-y-0.5 pl-7">
          <li
            v-for="m in rowIssues(i, 'error')"
            :key="m"
            class="text-[11px] font-medium text-red-600"
          >
            {{ m }}
          </li>
          <li v-for="m in rowIssues(i, 'warn')" :key="m" class="text-[11px] text-amber-600">
            {{ m }}
          </li>
        </ul>
      </div>
    </div>

    <div class="mt-2.5 flex flex-wrap items-center gap-2">
      <button
        class="rounded-md border border-dashed border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-500 transition hover:border-zinc-400 hover:text-zinc-900"
        @click="addPoint"
      >
        + 加折点
      </button>
      <button
        class="rounded-md border border-zinc-200 px-2.5 py-1 text-xs font-medium text-zinc-500 transition hover:border-zinc-300 hover:text-zinc-900"
        @click="sortByTemp"
      >
        按温度排序
      </button>
    </div>

    <!-- 全局参数 -->
    <div class="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs">
      <label class="flex items-center gap-2">
        <span class="text-zinc-500">滞回带</span>
        <input
          v-model.number="hysteresis"
          type="number"
          min="0"
          max="20"
          step="0.5"
          class="h-8 w-16 rounded-md border border-zinc-300 px-2 tnum focus:border-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
          @change="touch"
        />
        <span class="text-zinc-400">°C</span>
      </label>
      <label class="flex items-center gap-2">
        <span class="text-zinc-500">占空比下限</span>
        <input
          v-model.number="minDuty"
          type="number"
          min="1"
          max="100"
          class="h-8 w-16 rounded-md border border-zinc-300 px-2 tnum focus:border-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
          @change="touch"
        />
        <span class="text-zinc-400">%</span>
      </label>
      <label class="flex items-center gap-2">
        <span class="text-zinc-500">上限</span>
        <input
          v-model.number="maxDuty"
          type="number"
          min="1"
          max="100"
          class="h-8 w-16 rounded-md border border-zinc-300 px-2 tnum focus:border-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
          @change="touch"
        />
        <span class="text-zinc-400">%</span>
      </label>
    </div>

    <!-- 语义说明：这三个点是最容易理解错的 -->
    <ul class="mt-3 space-y-1 rounded-lg bg-zinc-50 px-3 py-2 text-[11px] text-zinc-500 ring-1 ring-inset ring-zinc-200">
      <li v-if="firstPoint">
        · 温度低于第一个折点（{{ firstPoint.temp.toFixed(0) }}°C）时用<b>第一档
        {{ firstPoint.duty }}%</b>，不是下限 {{ minDuty }}% —— 下限只在折点低于它时才起作用
      </li>
      <li>· 阶梯语义：59°C 没到 60°C 折点就<b>不</b>升档（保守方向），升温会立即升档不延迟</li>
      <li>· 滞回带 {{ hysteresis }}°C：降温要跌到「折点温度 − 滞回带」以下才降档，防转速横跳</li>
    </ul>

    <p v-if="globalErrors.length" class="mt-2 space-y-0.5">
      <span
        v-for="m in globalErrors"
        :key="m"
        class="block text-[11px] font-medium text-red-600"
      >
        {{ m }}
      </span>
    </p>
  </div>
</template>
