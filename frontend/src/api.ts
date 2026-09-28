import type {
  AssignmentItem,
  CurvePoint,
  CurvePreviewResponse,
  HistoryResponse,
  RuntimeSettings,
  SettingsPatch,
  StatusSnapshot,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!resp.ok) {
    // 把后端的 detail 原样带出来 —— 排障时这句话比状态码有用得多
    const text = await resp.text().catch(() => '')
    throw new Error(text || `HTTP ${resp.status}`)
  }
  return (await resp.json()) as T
}

export const api = {
  status: () => request<StatusSnapshot>('/api/status'),

  setMode: (mode: 'auto' | 'manual') =>
    request<{ ok: boolean; mode: string }>('/api/mode', {
      method: 'POST',
      body: JSON.stringify({ mode }),
    }),

  /** duty 传 null = 把该风扇位交回 BMC 自动控制 */
  setManualDuty: (slot: string, duty: number | null) =>
    request<{ ok: boolean; slot: string; duty: number | null }>('/api/manual', {
      method: 'POST',
      body: JSON.stringify({ slot, duty }),
    }),

  /** 紧急回落：把所有风扇位交回 BMC。安全方向操作，任何时候都可调用 */
  restoreAuto: () =>
    request<{ ok: boolean; output: string; rc: number }>('/api/restore-auto', {
      method: 'POST',
    }),

  /** 历史趋势：后端代理 Prometheus query_range，一次拿回全部曲线 */
  history: (minutes = 30) =>
    request<HistoryResponse>(`/api/history?minutes=${minutes}`),

  /**
   * 更新「散热源 → 风扇位」的分配（GPU 为主体，它挑自己的风扇接口）。
   *
   * ⚠️ **全量语义** —— 传的是完整列表，后端会整体覆盖，所以每次都要把
   * 所有源带上（只传一个会把其它的丢掉）。
   */
  setAssignments: (assignments: AssignmentItem[]) =>
    request<{ ok: boolean; assignments: AssignmentItem[] }>('/api/assignments', {
      method: 'PUT',
      body: JSON.stringify({ assignments }),
    }),

  /**
   * 试算一条**还没保存**的曲线。
   *
   * 走后端真实算法（阶梯语义 + 上下限钳制），保证「预览到的」就是「保存后会跑的」。
   * 顺带承担校验：折点温度重复 / 占空比越界 / 上下限倒置都会撞成 400，
   * 错误信息就是 ``detail`` 原文，可以直接显示给用户。
   */
  previewCurve: (payload: {
    points: CurvePoint[]
    hysteresis: number
    min_duty: number
    max_duty: number
    from: number
    to: number
    step: number
  }) =>
    request<CurvePreviewResponse>('/api/curve/preview', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 操作审计（IPMI 写入 / API 调用 / 启停） */
  audit: (limit = 100) =>
    request<{ records: AuditRecord[] }>(`/api/audit?limit=${limit}`),

  /** 读取运行时设置（值来自 SQLite，界面上改过的即权威值） */
  getSettings: () => request<RuntimeSettings>('/api/settings'),

  /**
   * 修改运行时设置（**部分更新**，只传要改的键）。
   * 改完立即生效，不需要重启服务。
   */
  patchSettings: (patch: SettingsPatch) =>
    request<{ ok: boolean; settings: RuntimeSettings }>('/api/settings', {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),
}

export interface AuditRecord {
  id: number
  ts: number
  kind: string
  actor: string
  detail: string
  ok: boolean
}
