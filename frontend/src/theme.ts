/**
 * ECharts 主题 token —— 与 Tailwind 的 zinc/语义色同源。
 * 两个图表（趋势 / 曲线）共用，保证整页图表一个味儿。
 */

export const CHART = {
  /** 文字：zinc-500 的近亲 */
  text: '#71717a',
  /** 弱文字：zinc-400 */
  textFaint: '#a1a1aa',
  /** 网格线：zinc-100 */
  grid: '#f4f4f5',
  /** 分割轴：zinc-200 */
  axis: '#e4e4e7',
  /** tooltip 背景 */
  tooltipBg: '#ffffff',
  tooltipBorder: '#e4e4e7',

  /**
   * 系列色板 —— 温度暖色、转速冷色的语义保留：
   * GPU 温度橙/红、CPU 温度琥珀、风扇转速蓝/青，够区分且不刺眼。
   */
  series: ['#f97316', '#dc2626', '#f59e0b', '#0ea5e9', '#6366f1', '#14b8a6', '#a855f7'],
} as const

/** 两张图共用的公共片段（网格 / 坐标轴 / tooltip 基调） */
export const chartBase = {
  animation: false,
  textStyle: { fontFamily: 'inherit' },
  grid: { left: 52, right: 56, top: 36, bottom: 28 },
  legend: {
    top: 0,
    left: 0,
    itemWidth: 12,
    itemHeight: 2,
    itemGap: 14,
    icon: 'rect',
    textStyle: { fontSize: 11, color: CHART.text },
  },
  tooltip: {
    trigger: 'axis' as const,
    backgroundColor: CHART.tooltipBg,
    borderColor: CHART.tooltipBorder,
    borderWidth: 1,
    padding: [8, 12],
    textStyle: { fontSize: 11, color: '#18181b' },
    extraCssText: 'box-shadow: 0 4px 12px rgba(0,0,0,.08); border-radius: 8px;',
    axisPointer: { type: 'line' as const, lineStyle: { color: CHART.axis } },
  },
  axisLabel: { fontSize: 11, color: CHART.textFaint },
  splitLine: { lineStyle: { color: CHART.grid } },
}
