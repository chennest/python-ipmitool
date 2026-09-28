<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api'
import type { CurvePoint, GpuInfo, RuntimeSettings, SettingsPatch } from '../types'

const props = defineProps<{
  settings: RuntimeSettings | null
  /** 实时 GPU 列表 —— 供「管控 GPU」勾选（WS 推送保持新鲜） */
  gpus: GpuInfo[]
}>()

const saving = ref(false)
const message = ref<string | null>(null)
const error = ref<string | null>(null)

/** 本地草稿：整份设置的可编辑副本 */
const draft = ref<{
  enabled: boolean
  interval: number
  managedGpus: string[]
  points: CurvePoint[]
  hysteresis: number
  minDuty: number
  maxDuty: number
  emergencyTemp: number
  emergencyResume: number
} | null>(null)

function syncFromProps(s: RuntimeSettings | null) {
  if (!s) return
  const stored = s['control.managed_gpus'] ?? []
  draft.value = {
    enabled: s['control.enabled'],
    interval: s['control.interval'],
    // 空列表语义 = 「全部管控」—— UI 上显示为全部勾选，保存时再还原成 []
    managedGpus: stored.length ? [...stored] : props.gpus.map((g) => g.uuid),
    points: s.curve.points.map((p) => ({ ...p })),
    hysteresis: s.curve.hysteresis,
    minDuty: s.curve.min_duty,
    maxDuty: s.curve.max_duty,
    emergencyTemp: s['safety.emergency_temp'],
    emergencyResume: s['safety.emergency_resume_temp'],
  }
}

// 只在首次拿到数据时同步，避免用户在编辑时被 2 秒一次的推送冲掉
watch(() => props.settings, (s) => { if (!draft.value) syncFromProps(s) }, {
  immediate: true,
})

const dirty = ref(false)
function touch() {
  dirty.value = true
}

function addPoint() {
  if (!draft.value) return
  const pts = draft.value.points
  const last = pts.length ? pts[pts.length - 1] : { temp: 60, duty: 50 }
  pts.push({ temp: Math.min(110, last.temp + 10), duty: Math.min(100, last.duty + 15) })
  touch()
}

function removePoint(i: number) {
  if (!draft.value || draft.value.points.length <= 1) return
  draft.value.points.splice(i, 1)
  touch()
}

const canSave = computed(() => draft.value !== null && dirty.value && !saving.value)

/** 是否全部已知 GPU 都被勾选（决定保存成 [] 还是显式列表） */
const allManaged = computed(() => {
  const d = draft.value
  if (!d || !props.gpus.length) return false
  return props.gpus.every((g) => d.managedGpus.includes(g.uuid))
})

/** 「管控 GPU」勾选切换 */
function toggleManaged(uuid: string) {
  if (!draft.value) return
  const list = draft.value.managedGpus
  const i = list.indexOf(uuid)
  if (i >= 0) list.splice(i, 1)
  else list.push(uuid)
  touch()
}

async function save() {
  if (!draft.value) return
  saving.value = true
  message.value = null
  error.value = null
  try {
    // 全部已知 GPU 都勾选 → 存 []（= 全部管控，新插的卡自动纳入）
    const allSelected =
      props.gpus.length > 0 &&
      props.gpus.every((g) => draft.value!.managedGpus.includes(g.uuid))
    const patch: SettingsPatch = {
      control_enabled: draft.value.enabled,
      control_interval: draft.value.interval,
      managed_gpus: allSelected ? [] : draft.value.managedGpus,
      curve: {
        points: draft.value.points,
        hysteresis: draft.value.hysteresis,
        min_duty: draft.value.minDuty,
        max_duty: draft.value.maxDuty,
      },
      emergency_temp: draft.value.emergencyTemp,
      emergency_resume_temp: draft.value.emergencyResume,
    }
    await api.patchSettings(patch)
    dirty.value = false
    message.value = '设置已保存并立即生效（无需重启）'
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

function reset() {
  syncFromProps(props.settings)
  dirty.value = false
  message.value = null
  error.value = null
}
</script>

<template>
  <section
    v-if="draft"
    class="rounded-xl border border-zinc-200 bg-white"
  >
    <!-- 卡片头 -->
    <div class="flex flex-wrap items-center gap-x-3 border-b border-zinc-100 px-4 py-3">
      <div class="mr-auto">
        <h2 class="text-[13px] font-semibold text-zinc-900">设置</h2>
        <p class="mt-0.5 text-[11px] text-zinc-400">
          管控哪些 GPU · 调速曲线 · 安全阈值 —— 保存后立即生效，值存在后端数据库
        </p>
      </div>
      <span
        v-if="dirty"
        class="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2.5 py-1 text-[11px] font-medium text-amber-700 ring-1 ring-inset ring-amber-600/10"
      >
        <span class="size-1.5 rounded-full bg-amber-500" />
        有未保存的修改
      </span>
    </div>

    <div class="divide-y divide-zinc-100">
      <!-- 管控 GPU -->
      <div class="p-4">
        <div class="mb-3 flex items-baseline gap-2">
          <h3 class="text-xs font-semibold text-zinc-900">管控 GPU</h3>
          <span class="text-[11px] text-zinc-400">
            勾选参与风扇联动的卡；取消的卡即使有分配也不会被驱动，交回 BMC
          </span>
        </div>

        <p
          v-if="!gpus.length"
          class="rounded-lg bg-zinc-50 px-3 py-2 text-xs text-zinc-400 ring-1 ring-inset ring-zinc-200"
        >
          读不到 GPU 列表（DCGM 未就绪？）—— 就绪后这里会列出所有卡
        </p>

        <ul v-else class="space-y-1.5">
          <li
            v-for="g in gpus"
            :key="g.uuid"
            class="flex items-center justify-between rounded-lg border border-zinc-200 px-3 py-2 transition hover:border-zinc-300"
          >
            <label class="flex cursor-pointer items-center gap-2.5">
              <input
                type="checkbox"
                class="h-4 w-4 accent-zinc-900"
                :checked="draft?.managedGpus.includes(g.uuid) ?? false"
                @change="toggleManaged(g.uuid)"
              />
              <span class="text-xs font-medium text-zinc-900 tnum">
                {{ g.short_uuid }}
              </span>
              <span class="text-[11px] text-zinc-400">{{ g.model }}</span>
            </label>
            <span class="text-[11px] text-zinc-400 tnum">
              {{ g.temperature !== null ? `${g.temperature.toFixed(1)}°C` : '—' }}
            </span>
          </li>
        </ul>

        <p v-if="gpus.length" class="mt-2 text-[11px] text-zinc-400">
          {{
            allManaged
              ? '当前管控全部 GPU —— 保存后新插入的卡也会自动纳入'
              : `当前只管控 ${draft?.managedGpus.length ?? 0} / ${gpus.length} 张卡`
          }}
        </p>
      </div>

      <!-- 控制开关与周期 -->
      <div class="grid gap-4 p-4 sm:grid-cols-2">
        <label
          class="flex cursor-pointer items-center justify-between rounded-lg border border-zinc-200 px-3 py-2.5 transition hover:border-zinc-300"
        >
          <span>
            <span class="block text-xs font-medium text-zinc-900">控制总开关</span>
            <span class="mt-0.5 block text-[11px] text-zinc-400">
              {{ draft.enabled ? '开启 —— 按曲线自动调档' : '关闭 —— 完全不碰风扇' }}
            </span>
          </span>
          <input
            v-model="draft.enabled"
            type="checkbox"
            class="h-4 w-4 accent-zinc-900"
            @change="touch"
          />
        </label>

        <label
          class="flex items-center justify-between rounded-lg border border-zinc-200 px-3 py-2.5 transition hover:border-zinc-300"
        >
          <span class="text-xs font-medium text-zinc-900">控制周期</span>
          <span class="flex items-center gap-1.5">
            <input
              v-model.number="draft.interval"
              type="number"
              min="1"
              max="3600"
              class="h-8 w-20 rounded-md border border-zinc-300 px-2 text-xs tnum focus:border-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
              @change="touch"
            />
            <span class="text-[11px] text-zinc-400">秒</span>
          </span>
        </label>
      </div>

      <!-- 曲线 -->
      <div class="p-4">
        <div class="mb-3 flex items-baseline gap-2">
          <h3 class="text-xs font-semibold text-zinc-900">温度 → 占空比曲线</h3>
          <span class="text-[11px] text-zinc-400">
            温度<b>达到</b>折点才升档；降温要跌出滞回带才降档
          </span>
        </div>

        <div class="space-y-1.5">
          <div
            v-for="(p, i) in draft.points"
            :key="i"
            class="flex items-center gap-2 text-xs"
          >
            <span class="w-5 text-center text-[11px] text-zinc-300">{{ i + 1 }}</span>
            <input
              v-model.number="p.temp"
              type="number"
              class="h-8 w-20 rounded-md border border-zinc-300 px-2 tnum focus:border-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
              @change="touch"
            />
            <span class="text-zinc-400">°C →</span>
            <input
              v-model.number="p.duty"
              type="number"
              min="0"
              max="100"
              class="h-8 w-20 rounded-md border border-zinc-300 px-2 tnum focus:border-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
              @change="touch"
            />
            <span class="text-zinc-400">%</span>
            <button
              class="ml-auto rounded-md px-2 py-1 text-[11px] font-medium text-zinc-400 transition hover:bg-red-50 hover:text-red-600 disabled:opacity-30"
              :disabled="draft.points.length <= 1"
              @click="removePoint(i)"
            >
              删除
            </button>
          </div>
        </div>

        <button
          class="mt-2.5 rounded-md border border-dashed border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-500 transition hover:border-zinc-400 hover:text-zinc-900"
          @click="addPoint"
        >
          + 加折点
        </button>

        <div class="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs">
          <label class="flex items-center gap-2">
            <span class="text-zinc-500">滞回带</span>
            <input
              v-model.number="draft.hysteresis"
              type="number"
              min="0"
              class="h-8 w-16 rounded-md border border-zinc-300 px-2 tnum focus:border-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
              @change="touch"
            />
            <span class="text-zinc-400">°C</span>
          </label>
          <label class="flex items-center gap-2">
            <span class="text-zinc-500">占空比下限</span>
            <input
              v-model.number="draft.minDuty"
              type="number"
              min="1"
              max="100"
              class="h-8 w-16 rounded-md border border-zinc-300 px-2 tnum focus:border-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
              @change="touch"
            />
            <span class="text-zinc-400">%</span>
          </label>
          <label class="flex items-center gap-2">
            <span class="text-zinc-500">上限</span>
            <input
              v-model.number="draft.maxDuty"
              type="number"
              min="1"
              max="100"
              class="h-8 w-16 rounded-md border border-zinc-300 px-2 tnum focus:border-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
              @change="touch"
            />
            <span class="text-zinc-400">%</span>
          </label>
        </div>
      </div>

      <!-- 安全阈值 -->
      <div class="p-4">
        <h3 class="mb-3 text-xs font-semibold text-zinc-900">安全阈值</h3>
        <div class="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs">
          <label class="flex items-center gap-2">
            <span class="text-zinc-500">紧急拉满</span>
            <input
              v-model.number="draft.emergencyTemp"
              type="number"
              class="h-8 w-16 rounded-md border border-zinc-300 px-2 tnum focus:border-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
              @change="touch"
            />
            <span class="text-zinc-400">°C</span>
          </label>
          <label class="flex items-center gap-2">
            <span class="text-zinc-500">解除</span>
            <input
              v-model.number="draft.emergencyResume"
              type="number"
              class="h-8 w-16 rounded-md border border-zinc-300 px-2 tnum focus:border-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
              @change="touch"
            />
            <span class="text-zinc-400">°C</span>
          </label>
          <span class="text-[11px] text-zinc-400">
            超过「紧急拉满」立即 100%，降到「解除」以下才恢复曲线控制
          </span>
        </div>
      </div>
    </div>

    <!-- 底部操作条 -->
    <div class="flex items-center gap-2 border-t border-zinc-100 bg-zinc-50/60 px-4 py-3">
      <p v-if="message" class="mr-auto text-xs font-medium text-emerald-600">
        {{ message }}
      </p>
      <p v-else-if="error" class="mr-auto text-xs font-medium text-red-600">
        {{ error }}
      </p>
      <span v-else class="mr-auto" />
      <button
        class="h-8 rounded-lg border border-zinc-200 bg-white px-3 text-xs font-medium text-zinc-600 transition hover:bg-zinc-50 disabled:opacity-40"
        :disabled="!dirty || saving"
        @click="reset"
      >
        撤销修改
      </button>
      <button
        class="h-8 rounded-lg px-4 text-xs font-medium transition"
        :class="
          canSave
            ? 'bg-zinc-900 text-white hover:bg-zinc-800'
            : 'cursor-not-allowed bg-zinc-200 text-zinc-400'
        "
        :disabled="!canSave"
        @click="save"
      >
        {{ saving ? '保存中…' : '保存设置' }}
      </button>
    </div>
  </section>
</template>
