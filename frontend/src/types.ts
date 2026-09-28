/**
 * 与后端 app/controller.py 的 describe() 输出一一对应。
 * 改后端字段时记得同步这里。
 */

export interface GpuInfo {
  uuid: string
  /** 短 uuid，如 e49ed30f —— 界面上用这个，完整 uuid 太长 */
  short_uuid: string
  index: number
  pci_bus_id: string
  model: string
  temperature: number | null
  power_watts: number | null
  utilization: number | null
  memory_used_mib: number | null
  memory_total_mib: number | null
  memory_percent: number | null
}

export interface FanInfo {
  slot: string
  /** 当前下发的占空比；null 表示该位交回 BMC 自动 */
  duty: number | null
  temperature: number | null
  curve_index: number | null
  updated_ts: number | null
  rpm: number | null
  /** 温度源类型：gpu / gpu_group / cpu */
  source: string
  /** 占用这个风扇位的源的 key（空 = 没被分配，程序不接管） */
  owner_key: string
  /** 绑定的 GPU UUID（一对一绑定时有值） */
  bound_uuids: string[]
  /** 人话说明：这一路温度是从哪儿来的（后端算好后给） */
  bound_detail: string
}

/**
 * 一个散热源的风扇分配 —— **主体是源（GPU），它去挑风扇接口**。
 * kind: gpu = 某张具体的卡；cpu = CPU 核温度
 */
export interface AssignmentItem {
  key: string
  kind: 'gpu' | 'cpu'
  gpu_uuid: string | null
  /** 展示用的标签（后端拼好） */
  label: string
  slots: string[]
  temperature: number | null
  online: boolean
  /** 是否纳入管控（设置页可勾选；false = 分配保留但不驱动） */
  managed: boolean
}

/** 可选的绑定目标 */
export interface BindingOption {
  uuid: string
  short_uuid: string
  label: string
}

/** 可分配的风扇位（全部列出，没有转速读数的也在，rpm=null） */
export interface FanSlotOption {
  slot: string
  rpm: number | null
}

export interface CurvePoint {
  temp: number
  duty: number
}

export interface CurveInfo {
  points: CurvePoint[]
  hysteresis: number
  min_duty: number
  max_duty: number
}

/** CPU 核心温度（node_exporter 的 hwmon）：Tctl / Tccd* */
export interface CPUCoreTemp {
  label: string
  celsius: number | null
}

/** BMC 板载温度传感器：MB Temp / CPU Temp / Card Side Temp / DDR4_* */
export interface BoardTemp {
  name: string
  celsius: number | null
  state: string
}

export interface TemperatureInfo {
  cpu_cores: CPUCoreTemp[]
  board: BoardTemp[]
  sources: { cpu: string; board: string }
}

/** 运行时设置（可在界面上改，落 SQLite；配置文件只提供初始值） */
export interface RuntimeSettings {
  'control.enabled': boolean
  'control.interval': number
  /** 管控的 GPU UUID 列表；空数组 = 全部管控（保守默认） */
  'control.managed_gpus': string[]
  curve: CurveInfo
  'safety.emergency_temp': number
  'safety.emergency_resume_temp': number
}

/** 部分更新用的补丁（只传要改的键） */
export interface SettingsPatch {
  control_enabled?: boolean
  control_interval?: number
  managed_gpus?: string[]
  curve?: CurveInfo
  emergency_temp?: number
  emergency_resume_temp?: number
}

export interface StatusSnapshot {
  running: boolean
  mode: 'auto' | 'manual'
  interval: number
  emergency: boolean
  last_tick_ts: number | null
  last_error: string | null
  consecutive_failures: number
  sources: { gpu: string; fan: string }
  gpus: GpuInfo[]
  fans: FanInfo[]
  curve: CurveInfo
  ipmi_target: Record<string, number | null>
  /** CPU 核温度 + BMC 板载温度。⚠️ GPU 温度在 gpus[] 里，不在这 */
  temperatures: TemperatureInfo
  /** 「散热源 → 风扇位」的分配（主体是源，界面上按 GPU 一行一行选风扇） */
  assignments: AssignmentItem[]
  /** 可分配的风扇位（全部列出，没读数的也在；rpm 供展示） */
  fan_slots: FanSlotOption[]
  /**
   * false = 首次使用，分配还没配过。
   * 此时后端**不会调档**，前端必须弹分配向导让用户自己指定。
   */
  bindings_configured: boolean
  /** 可供绑定的目标（GPU 列表 + CPU 选项） */
  binding_options: {
    gpus: BindingOption[]
    cpu_label: string
  }
  /** 当前生效的运行时设置 */
  settings: RuntimeSettings
}

/** 历史曲线中的一条 */
export interface HistorySeriesItem {
  key: string
  label: string
  unit: string
  /** 挂在哪个 Y 轴上 —— 温度和转速量纲不同，必须分轴 */
  axis: 'left' | 'right'
  /** [unix 毫秒, 值] */
  points: [number, number][]
}

/**
 * 历史数据响应。
 *
 * 一次请求把图上所有曲线都取回来 —— 按指标分开请求会让前端发好几次 HTTP，
 * 还得自己对齐时间轴，没有必要。
 *
 * 数据由后端代理 Prometheus 的 `query_range` 得到，浏览器不直连 Prometheus
 * （避免跨域，也别把地址暴露出去）。
 */
export interface HistoryResponse {
  minutes: number
  series: HistorySeriesItem[]
}
