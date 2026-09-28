<script setup lang="ts">
import GpuCards from '../components/GpuCards.vue'
import TemperaturePanel from '../components/TemperaturePanel.vue'
import TrendChart from '../components/TrendChart.vue'
import type { AssignmentItem, StatusSnapshot } from '../types'

defineProps<{
  snapshot: StatusSnapshot | null
  assignments: AssignmentItem[]
}>()
</script>

<template>
  <div class="space-y-4">
    <GpuCards :gpus="snapshot?.gpus ?? []" :assignments="assignments" />
    <TemperaturePanel :temperatures="snapshot?.temperatures ?? null" />
    <TrendChart />
  </div>
</template>
