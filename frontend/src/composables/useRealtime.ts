import { onUnmounted, ref } from 'vue'
import { api } from '../api'
import type { StatusSnapshot } from '../types'

/**
 * 实时状态订阅。
 *
 * **WebSocket 为主，轮询兜底**：WS 断了之后立刻降级成 5 秒一次轮询，
 * 同时每 3 秒尝试重连 —— 界面不该因为 WS 抖动就停摆。
 *
 * 注意后端推的是**完整快照**（2 秒一次），不是增量。快照很小（两张卡 +
 * 四个风扇位），全量推送上位比维护增量状态简单得多，也不会有状态不一致。
 */
export function useRealtime() {
  const snapshot = ref<StatusSnapshot | null>(null)
  const connected = ref(false)
  const lastError = ref<string | null>(null)
  /** 上一次成功收到数据的时间戳，用来判断数据是否"不新鲜了" */
  const lastUpdate = ref<number | null>(null)

  let ws: WebSocket | null = null
  let reconnectTimer: number | null = null
  let pollTimer: number | null = null
  let disposed = false

  function wsUrl(): string {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${proto}//${location.host}/api/ws`
  }

  function connect(): void {
    if (disposed) return
    try {
      ws = new WebSocket(wsUrl())
    } catch {
      scheduleReconnect()
      return
    }

    ws.onopen = () => {
      connected.value = true
      lastError.value = null
      stopPolling()
    }

    ws.onmessage = (ev: MessageEvent<string>) => {
      try {
        snapshot.value = JSON.parse(ev.data) as StatusSnapshot
        lastUpdate.value = Date.now()
      } catch {
        /* 坏帧忽略，别把整个界面搞崩 */
      }
    }

    ws.onclose = () => {
      connected.value = false
      scheduleReconnect()
    }

    ws.onerror = () => {
      lastError.value = 'WebSocket 连接异常'
      // 具体重连交给随后的 onclose
    }
  }

  function scheduleReconnect(): void {
    if (disposed || reconnectTimer !== null) return
    startPolling()
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      connect()
    }, 3000)
  }

  function startPolling(): void {
    if (pollTimer !== null) return
    pollTimer = window.setInterval(() => {
      void (async () => {
        try {
          snapshot.value = await api.status()
          lastUpdate.value = Date.now()
          lastError.value = null
        } catch (e) {
          lastError.value = e instanceof Error ? e.message : String(e)
        }
      })()
    }, 5000)
  }

  function stopPolling(): void {
    if (pollTimer !== null) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  function close(): void {
    disposed = true
    stopPolling()
    if (reconnectTimer !== null) clearTimeout(reconnectTimer)
    ws?.close()
  }

  /** 主动拉一次 —— 用户刚做完调控操作时调用，别干等下一个推送周期 */
  async function refresh(): Promise<void> {
    try {
      snapshot.value = await api.status()
      lastUpdate.value = Date.now()
      lastError.value = null
    } catch (e) {
      lastError.value = e instanceof Error ? e.message : String(e)
    }
  }

  connect()
  onUnmounted(close)

  return { snapshot, connected, lastError, lastUpdate, refresh }
}
