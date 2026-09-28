<script setup lang="ts">
import BindingWizard from '../components/BindingWizard.vue'
import FanPanel from '../components/FanPanel.vue'
import CurveChart from '../components/CurveChart.vue'
import type { FanInfo, StatusSnapshot } from '../types'

defineProps<{
  snapshot: StatusSnapshot | null
}>()

const emit = defineEmits<{ (e: 'refresh'): void }>()

function asFans(s: StatusSnapshot | null): FanInfo[] {
  return s?.fans ?? []
}
</script>

<template>
  <div class="space-y-4">
    <BindingWizard
      v-if="snapshot"
      :assignments="snapshot.assignments"
      :fan-slots="snapshot.fan_slots"
      :initial="!snapshot.bindings_configured"
      @saved="emit('refresh')"
    />

    <FanPanel
      :fans="asFans(snapshot)"
      :mode="snapshot?.mode ?? 'auto'"
      @refresh="emit('refresh')"
    />

    <CurveChart :curve="snapshot?.curve ?? null" :fans="asFans(snapshot)" />
  </div>
</template>
