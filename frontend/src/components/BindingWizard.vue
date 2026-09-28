<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { api } from '../api'
import type { AssignmentItem, FanSlotOption } from '../types'
import { fmt } from '../utils'

const props = defineProps<{
  assignments: AssignmentItem[]
  fanSlots: FanSlotOption[]
  /** true = 首次使用（库是空的），顶部显示警示条；false = 常驻编辑面板 */
  initial?: boolean
}>()

const emit = defineEmits<{ (e: 'saved'): void }>()

/** 每个源选了哪个风扇位：key → slot（'' = 不分配，交回 BMC） */
const selection = reactive<Record<string, string>>({})
/**
 * ⚠️ 只在「分配内容真正变化」时才重建 selection —— WS 每 2 秒推一次快照，
 * 数组每次都是新对象；如果直接 watch 数组引用，用户正在下拉框里选的值
 * 会被 2 秒后的推送冲掉。
 */
const assignmentSignature = computed(() =>
  props.assignments.map((a) => `${a.key}=${a.slots.join(',')}`).join('|'),
)
watch(assignmentSignature, () => {
  for (const key of Object.keys(selection)) delete selection[key]
  for (const a of props.assignments) {
    // 未纳入管控的卡不能持有分配（与后端校验一致）
    selection[a.key] = a.managed ? (a.slots[0] ?? '') : ''
  }
}, { immediate: true })

const saving = ref(false)
const error = ref<string | null>(null)
/** 跨源转移提示（选了别人占用的位时告知用户） */
const notice = ref<string | null>(null)

const labelOf = computed(() => {
  const map: Record<string, string> = {}
  for (const f of props.fanSlots) {
    map[f.slot] =
      f.rpm === null ? `${f.slot}（无读数）` : `${f.slot}（${Math.round(f.rpm)} RPM）`
  }
  return map
})

/** 被占用的位数量 —— 至少占一个才允许保存 */
const assignedCount = computed(
  () => new Set(Object.values(selection).filter(Boolean)).size,
)

/** 该位当前被哪个源占用（下拉框里给「已被占用」提示） */
function ownerOf(slot: string): string | null {
  for (const [key, chosen] of Object.entries(selection)) {
    if (chosen === slot) return key
  }
  return null
}

function onPick(key: string, ev: Event) {
  const a = props.assignments.find((x) => x.key === key)
  if (a && !a.managed) return // 未管控的卡禁用，防御性兜底
  const slot = (ev.target as HTMLSelectElement).value
  notice.value = null
  if (slot) {
    // 一个风扇位只能给一个源：选了别人占的，那边自动改回「不分配」
    for (const other of Object.keys(selection)) {
      if (other !== key && selection[other] === slot) {
        selection[other] = ''
        notice.value = `「${slot}」已从 ${other} 转移给 ${key} —— 一个风扇位只能属于一个散热源`
      }
    }
  }
  selection[key] = slot
}

async function save() {
  saving.value = true
  error.value = null
  try {
    await api.setAssignments(
      props.assignments.map((a) => ({
        key: a.key,
        kind: a.kind,
        gpu_uuid: a.gpu_uuid,
        label: a.label,
        // 未纳入管控的卡强制不分配 —— 与设置页联动
        slots: a.managed && selection[a.key] ? [selection[a.key]] : [],
        temperature: a.temperature,
        online: a.online,
        managed: a.managed,
      })),
    )
    emit('saved')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="rounded-xl border border-zinc-200 bg-white">
    <!-- 首次使用的警示条 -->
    <div
      v-if="initial"
      class="flex items-start gap-2.5 rounded-t-xl border-b border-amber-600/10 bg-amber-50 px-4 py-3"
    >
      <svg viewBox="0 0 16 16" class="mt-0.5 size-4 shrink-0 text-amber-600" fill="currentColor">
        <path
          fill-rule="evenodd"
          d="M8 15A7 7 0 1 0 8 1a7 7 0 0 0 0 14Zm0-11a.75.75 0 0 1 .75.75v4.5a.75.75 0 0 1-1.5 0v-4.5A.75.75 0 0 1 8 4Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
          clip-rule="evenodd"
        />
      </svg>
      <div class="text-xs leading-relaxed text-amber-900">
        <p class="font-semibold">首次使用 · 给每张 GPU 挑它的风扇接口</p>
        <p class="mt-0.5 text-amber-800">
          程序<b>不会替你猜</b>哪张卡用哪个风扇 —— 配好之前，控制器不会动任何风扇。
        </p>
      </div>
    </div>

    <!-- 卡片头 -->
    <div class="border-b border-zinc-100 px-4 py-3">
      <h2 class="text-[13px] font-semibold text-zinc-900">
        风扇分配（GPU → 风扇接口）
      </h2>
      <p class="mt-0.5 text-[11px] text-zinc-400">
        <b>GPU 是主体</b>：在每个源后面挑一个风扇接口，这张卡的温度就驱动它
      </p>
    </div>

    <ul class="divide-y divide-zinc-50">
      <li
        v-for="a in assignments"
        :key="a.key"
        class="flex flex-wrap items-center gap-3 px-4 py-3 transition hover:bg-zinc-50/60"
        :class="{ 'opacity-50': !a.online || (a.kind === 'gpu' && !a.managed) }"
      >
        <!-- 源 -->
        <div class="flex min-w-0 flex-1 items-center gap-2.5">
          <span
            class="inline-block size-2 shrink-0 rounded-full"
            :class="a.kind === 'gpu' ? 'bg-blue-500' : 'bg-zinc-300'"
          />
          <div class="min-w-0">
            <p class="flex items-center gap-2 truncate text-xs font-medium text-zinc-900" :title="a.label">
              {{ a.label }}
              <span
                v-if="a.kind === 'gpu' && !a.managed"
                class="shrink-0 rounded-full bg-zinc-100 px-2 py-0.5 text-[10px] font-medium text-zinc-500 ring-1 ring-inset ring-zinc-300"
              >
                未纳入管控
              </span>
            </p>
            <p v-if="a.kind === 'gpu' && a.temperature !== null" class="text-[11px] tnum text-zinc-400">
              {{ fmt(a.temperature, 1, '°C') }}
            </p>
            <p v-else-if="a.kind === 'gpu' && !a.managed" class="text-[11px] text-zinc-400">
              到「设置 → 管控 GPU」勾选后才能分配风扇
            </p>
            <p v-else-if="!a.online" class="text-[11px] font-medium text-red-500">
              掉卡中，无法读温度
            </p>
          </div>
        </div>

        <!-- 风扇接口选择框（未管控的卡禁用） -->
        <select
          class="h-8 w-56 rounded-lg border bg-white px-2 text-xs disabled:cursor-not-allowed disabled:opacity-60"
          :class="
            selection[a.key]
              ? 'border-zinc-900 bg-zinc-900 font-medium text-white'
              : 'border-zinc-300 text-zinc-600'
          "
          :value="selection[a.key] ?? ''"
          :disabled="a.kind === 'gpu' && !a.managed"
          :aria-label="`${a.label} 的风扇接口`"
          @change="onPick(a.key, $event)"
        >
          <option value="">{{ a.kind === 'gpu' && !a.managed ? '未纳入管控' : '不分配（交回 BMC 自动）' }}</option>
          <option v-for="f in fanSlots" :key="f.slot" :value="f.slot">
            {{ labelOf[f.slot] }}<template v-if="ownerOf(f.slot) && ownerOf(f.slot) !== a.key"> · 已被占用</template>
          </option>
        </select>
      </li>
    </ul>

    <p class="border-t border-zinc-100 px-4 py-2.5 text-[11px] leading-relaxed text-zinc-400">
      <b class="text-zinc-500">怎么确认哪张卡配哪个风扇？</b>
      单独降某一路转速、观察哪张卡的温度上升即可确认；
      没读数的位是未接线接口，选了也不会有风扇响应。
    </p>

    <!-- 底部操作条 -->
    <div class="flex items-center gap-2 border-t border-zinc-100 bg-zinc-50/60 px-4 py-3">
      <p v-if="notice" class="mr-auto text-xs font-medium text-blue-600">{{ notice }}</p>
      <p v-else-if="error" class="mr-auto text-xs font-medium text-red-600">{{ error }}</p>
      <span v-else class="mr-auto" />
      <button
        class="h-8 rounded-lg px-4 text-xs font-medium transition"
        :class="
          assignedCount > 0 && !saving
            ? 'bg-zinc-900 text-white hover:bg-zinc-800'
            : 'cursor-not-allowed bg-zinc-200 text-zinc-400'
        "
        :disabled="assignedCount === 0 || saving"
        @click="save"
      >
        <template v-if="saving">保存中…</template>
        <template v-else-if="assignedCount === 0">请先给散热源挑风扇接口</template>
        <template v-else>{{ initial ? '保存并开始监控' : '保存分配' }}</template>
      </button>
    </div>
  </section>
</template>
