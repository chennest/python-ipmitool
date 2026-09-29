import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

// 按需注册 —— 只引这个面板真正用到的，别把整个 echarts 打进来
echarts.use([
  LineChart,
  GridComponent,
  LegendComponent,
  MarkPointComponent,
  MarkLineComponent,
  MarkAreaComponent,
  TooltipComponent,
  CanvasRenderer,
])

export type EChartsInstance = echarts.ECharts

/** 把一个 DOM 元素变成自适应尺寸的 ECharts 实例（卸载时自动销毁） */
export function useChart(el: Ref<HTMLElement | null>) {
  const chart = ref<EChartsInstance | null>(null)
  let observer: ResizeObserver | null = null

  onMounted(() => {
    if (!el.value) return
    chart.value = echarts.init(el.value)

    // 用 ResizeObserver 而不是 window.resize：面板在栅格里，
    // 窗口没变但容器宽度也可能变（比如出现滚动条）
    observer = new ResizeObserver(() => chart.value?.resize())
    observer.observe(el.value)
  })

  onBeforeUnmount(() => {
    observer?.disconnect()
    chart.value?.dispose()
    chart.value = null
  })

  return chart
}
