<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRealtime } from './composables/useRealtime'
import Sidebar, { type PageKey } from './components/Sidebar.vue'
import TopBar from './components/TopBar.vue'
import DashboardPage from './pages/DashboardPage.vue'
import FansPage from './pages/FansPage.vue'
import SettingsPage from './pages/SettingsPage.vue'

const { snapshot, connected, refresh } = useRealtime()

const page = ref<PageKey>('dashboard')

const PAGE_META: Record<PageKey, { title: string; subtitle: string }> = {
  dashboard: { title: '概览', subtitle: 'GPU 状态 · 温度 · 历史趋势' },
  fans: { title: '风扇控制', subtitle: 'GPU → 风扇分配 · 手动调速 · 控制曲线' },
  settings: { title: '设置', subtitle: '控制开关 · 调速曲线 · 安全阈值' },
}

const meta = computed(() => PAGE_META[page.value])
</script>

<template>
  <div class="flex h-full min-h-screen flex-col md:flex-row">
    <Sidebar
      :page="page"
      :connected="connected"
      :emergency="snapshot?.emergency ?? false"
      @navigate="page = $event"
    />

    <div class="flex min-w-0 flex-1 flex-col">
      <TopBar
        :title="meta.title"
        :subtitle="meta.subtitle"
        :snapshot="snapshot"
        @refresh="refresh"
      />

      <main class="mx-auto w-full max-w-6xl flex-1 px-4 py-5 md:px-6">
        <DashboardPage
          v-if="page === 'dashboard'"
          :snapshot="snapshot"
          :assignments="snapshot?.assignments ?? []"
        />
        <FansPage
          v-else-if="page === 'fans'"
          :snapshot="snapshot"
          @refresh="refresh"
        />
        <SettingsPage v-else :snapshot="snapshot" />
      </main>

      <footer class="pb-4 text-center text-[11px] text-zinc-400">
        单机应用 · 观测走 exporter → Prometheus，调控走 ipmitool raw → BMC
      </footer>
    </div>
  </div>
</template>
