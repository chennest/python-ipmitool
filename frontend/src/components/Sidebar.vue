<script setup lang="ts">
/**
 * 侧边导航 —— 现代 SaaS 布局的骨架。
 * 宽屏：左侧常驻竖栏；窄屏：折叠成顶部横条。
 * 导航项 = 图标 + 标签（hint 只做 hover 提示，不上屏）。
 */
export type PageKey = 'dashboard' | 'fans' | 'settings'

defineProps<{
  page: PageKey
  connected: boolean
  emergency: boolean
}>()

const emit = defineEmits<{ (e: 'navigate', p: PageKey): void }>()

const NAV: { key: PageKey; label: string; hint: string }[] = [
  { key: 'dashboard', label: '概览', hint: 'GPU 状态 · 温度 · 趋势' },
  { key: 'fans', label: '风扇控制', hint: '分配 · 调速 · 曲线' },
  { key: 'settings', label: '设置', hint: '开关 · 阈值' },
]
</script>

<template>
  <aside
    class="flex shrink-0 flex-row items-center gap-2 border-b border-zinc-200 bg-white px-3 py-2 md:w-56 md:flex-col md:items-stretch md:gap-0 md:border-b-0 md:border-r md:py-4"
  >
    <!-- 品牌区 -->
    <div class="mr-auto flex items-center gap-2.5 md:mb-6 md:mr-0 md:px-3">
      <div
        class="flex size-8 items-center justify-center rounded-lg bg-zinc-900 text-white"
      >
        <svg viewBox="0 0 16 16" class="size-4" fill="currentColor">
          <path
            d="M8 8c0-3 .5-5.5 2.5-5.5S13 4.5 13 6.5 10.5 8 8 8Zm0 0c3 0 5.5.5 5.5 2.5S11.5 13 9.5 13 8 10.5 8 8Zm0 0c0 3-.5 5.5-2.5 5.5S3 11.5 3 9.5 5.5 8 8 8Zm0 0C5 8 2.5 7.5 2.5 5.5S4.5 3 6.5 3 8 5.5 8 8Z"
          />
        </svg>
      </div>
      <div class="leading-tight">
        <p class="text-[13px] font-semibold text-zinc-900">GPU 风扇控制台</p>
        <p class="hidden text-[11px] text-zinc-400 md:block">pve02 · 2× Tesla T10</p>
      </div>
    </div>

    <!-- 导航：图标 + 标签 -->
    <nav class="flex flex-row gap-1 md:flex-col md:px-2">
      <button
        v-for="item in NAV"
        :key="item.key"
        class="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] font-medium transition"
        :class="
          page === item.key
            ? 'bg-zinc-100 text-zinc-900'
            : 'text-zinc-500 hover:bg-zinc-50 hover:text-zinc-900'
        "
        :title="item.hint"
        @click="emit('navigate', item.key)"
      >
        <!-- 概览：四宫格 -->
        <svg
          v-if="item.key === 'dashboard'"
          viewBox="0 0 24 24"
          class="size-4 shrink-0"
          :class="page === item.key ? 'text-zinc-900' : 'text-zinc-400'"
          fill="none"
          stroke="currentColor"
          stroke-width="1.8"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path
            d="M3.75 6A2.25 2.25 0 0 1 6 3.75h2.25A2.25 2.25 0 0 1 10.5 6v2.25a2.25 2.25 0 0 1-2.25 2.25H6a2.25 2.25 0 0 1-2.25-2.25V6ZM13.5 6a2.25 2.25 0 0 1 2.25-2.25H18A2.25 2.25 0 0 1 20.25 6v2.25A2.25 2.25 0 0 1 18 10.5h-2.25a2.25 2.25 0 0 1-2.25-2.25V6ZM3.75 15.75A2.25 2.25 0 0 1 6 13.5h2.25a2.25 2.25 0 0 1 2.25 2.25V18a2.25 2.25 0 0 1-2.25 2.25H6A2.25 2.25 0 0 1 3.75 18v-2.25ZM13.5 15.75a2.25 2.25 0 0 1 2.25-2.25H18a2.25 2.25 0 0 1 2.25 2.25V18A2.25 2.25 0 0 1 18 20.25h-2.25A2.25 2.25 0 0 1 13.5 18v-2.25Z"
          />
        </svg>
        <!-- 风扇控制：三叶扇 -->
        <svg
          v-else-if="item.key === 'fans'"
          viewBox="0 0 16 16"
          class="size-4 shrink-0"
          :class="page === item.key ? 'text-zinc-900' : 'text-zinc-400'"
          fill="currentColor"
        >
          <path
            d="M8 8c0-3 .5-5.5 2.5-5.5S13 4.5 13 6.5 10.5 8 8 8Zm0 0c3 0 5.5.5 5.5 2.5S11.5 13 9.5 13 8 10.5 8 8Zm0 0c0 3-.5 5.5-2.5 5.5S3 11.5 3 9.5 5.5 8 8 8Zm0 0C5 8 2.5 7.5 2.5 5.5S4.5 3 6.5 3 8 5.5 8 8Z"
          />
        </svg>
        <!-- 设置：滑杆 -->
        <svg
          v-else
          viewBox="0 0 24 24"
          class="size-4 shrink-0"
          :class="page === item.key ? 'text-zinc-900' : 'text-zinc-400'"
          fill="none"
          stroke="currentColor"
          stroke-width="1.8"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path
            d="M10.5 6h9.75M10.5 6a1.5 1.5 0 1 1-3 0m3 0a1.5 1.5 0 1 0-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m-9.75 0h9.75"
          />
        </svg>

        <span>{{ item.label }}</span>

        <span
          v-if="item.key === 'fans' && emergency"
          class="ml-auto size-1.5 rounded-full bg-red-500"
          title="紧急散热中"
        />
      </button>
    </nav>

    <!-- 底部状态小面板 -->
    <div class="ml-auto md:mt-auto md:ml-2 md:block md:px-2">
      <div
        class="hidden rounded-lg border border-zinc-200 bg-zinc-50/80 p-3 text-[11px] leading-relaxed md:block"
      >
        <p class="flex items-center gap-1.5 font-medium" :class="connected ? 'text-emerald-700' : 'text-zinc-500'">
          <span
            class="size-1.5 rounded-full"
            :class="connected ? 'bg-emerald-500' : 'bg-zinc-400'"
          />
          {{ connected ? '实时连接' : '轮询模式' }}
        </p>
        <p class="mt-1 text-zinc-400 tnum">192.168.6.7:8765</p>
        <p
          v-if="emergency"
          class="mt-1.5 flex items-center gap-1.5 font-medium text-red-600"
        >
          <span class="size-1.5 animate-pulse rounded-full bg-red-500" />
          紧急散热中
        </p>
      </div>
    </div>
  </aside>
</template>
