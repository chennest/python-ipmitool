<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api'
import CurveEditor from './CurveEditor.vue'
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
  mode: 'auto' | 'manual'
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
    mode: s['control.mode'] ?? 'auto',
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

// ⚠️ 只在没有未保存修改（dirty）时跟随快照同步 —— 用户编辑中不被
// 2 秒一次的推送冲掉；但外部变化（顶栏切模式 / 别处改设置）必须反映
// 进来，否则设置页显示旧值、用户一点保存就把旧值写回去覆盖外部操作
// （2026-09-28 审计发现）。保存成功后 dirty=false，会自然回到跟随态。
watch(
  () => props.settings,
  (s) => {
    if (!dirty.value) syncFromProps(s)
  },
  { immediate: true },
)

const dirty = ref(false)
function touch() {
  dirty.value = true
}

/** 曲线编辑器回报的合法性 —— 有硬错误就别让用户点保存（点了也是 400） */
const curveValid = ref(true)

const canSave = computed(
  () => draft.value !== null && dirty.value && !saving.value && curveValid.value,
)

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
      control_mode: draft.value.mode,
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

      <!-- 控制开关 / 模式 / 周期 -->
      <div class="grid gap-4 p-4 sm:grid-cols-3">
        <label
          class="flex cursor-pointer items-center justify-between rounded-lg border border-zinc-200 px-3 py-2.5 transition hover:border-zinc-300"
        >
          <span>
            <span class="block text-xs font-medium text-zinc-900">控制总开关</span>
            <span class="mt-0.5 block text-[11px] text-zinc-400">
              <!-- ⚠️ 这里别写「按曲线自动调档」——那是「控制模式」的事，
                   两个概念都叫自动会把人绕晕 -->
              {{ draft.enabled ? '程序接管风扇（怎么定速看下面的模式）' : '程序完全不碰，全部交回 BMC 自动档' }}
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
          <span>
            <span class="block text-xs font-medium text-zinc-900">控制模式</span>
            <span class="mt-0.5 block text-[11px] text-zinc-400">
              {{
                draft.mode === 'auto'
                  ? '自动调档：按温度曲线算占空比'
                  : '手动定速：锁定你设的占空比，温度涨也不提速'
              }}
            </span>
          </span>
          <select
            v-model="draft.mode"
            class="h-8 rounded-md border border-zinc-300 bg-white px-2 text-xs focus:border-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
            @change="touch"
          >
            <option value="auto">自动调档</option>
            <option value="manual">手动定速</option>
          </select>
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

      <!-- 曲线：编辑器自带实时预览图 + 行内校验（数据走后端试算接口，
           保证「预览到的」就是「保存后会跑的」） -->
      <div class="p-4">
        <CurveEditor
          v-model:points="draft.points"
          v-model:hysteresis="draft.hysteresis"
          v-model:minDuty="draft.minDuty"
          v-model:maxDuty="draft.maxDuty"
          :gpus="gpus"
          :emergency-temp="draft.emergencyTemp"
          @change="touch"
          @validity="curveValid = $event"
        />
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
