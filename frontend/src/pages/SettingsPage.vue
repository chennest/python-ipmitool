<script setup lang="ts">
import SettingsPanel from '../components/SettingsPanel.vue'
import type { StatusSnapshot } from '../types'

defineProps<{ snapshot: StatusSnapshot | null }>()
</script>

<template>
  <div class="space-y-4">
    <SettingsPanel :settings="snapshot?.settings ?? null" :gpus="snapshot?.gpus ?? []" />
  </div>
</template>
