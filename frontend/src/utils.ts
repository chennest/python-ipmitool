/** 数值格式化与状态色阶 —— 多个组件共用 */

export function fmt(
  value: number | null | undefined,
  digits = 1,
  suffix = '',
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toFixed(digits) + suffix
}

export function fmtInt(value: number | null | undefined, suffix = ''): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return String(Math.round(value)) + suffix
}

/**
 * GPU 温度色阶。
 * Tesla T10 的 TjMax 约 89°C，且是**被动散热**（全靠机箱风扇吹），
 * 所以阈值给得保守：70°C 起警告、80°C 起告警。
 */
export function tempColor(t: number | null | undefined): string {
  if (t === null || t === undefined) return 'text-slate-400'
  if (t >= 80) return 'text-red-600'
  if (t >= 70) return 'text-amber-600'
  return 'text-emerald-600'
}

export function tempBarColor(t: number | null | undefined): string {
  if (t === null || t === undefined) return 'bg-slate-300'
  if (t >= 80) return 'bg-red-500'
  if (t >= 70) return 'bg-amber-500'
  return 'bg-emerald-500'
}

/** 距某个 unix 秒时间戳过去了多久（后端时间戳单位是秒） */
export function secondsSince(ts: number | null | undefined): number | null {
  if (!ts) return null
  return Math.round(Date.now() / 1000 - ts)
}

export function fmtAgo(ts: number | null | undefined): string {
  const s = secondsSince(ts)
  if (s === null) return '—'
  if (s < 60) return `${s} 秒前`
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  return `${Math.floor(s / 3600)} 小时前`
}
