<script setup lang="ts">
import GpuCards from '../components/GpuCards.vue'
import TemperaturePanel from '../components/TemperaturePanel.vue'
import TrendPanel from '../components/TrendPanel.vue'
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
    <TrendPanel />
  </div>
</template>
