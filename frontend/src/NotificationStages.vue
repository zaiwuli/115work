<script setup lang="ts">
import { computed } from 'vue'
type Stage = { id: string; label: string }
const props = defineProps<{ modelValue: Record<string, boolean>; catalog: Stage[]; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: Record<string, boolean>] }>()
const rows = [
  { title: '离线 / 种子下载', cells: [['offline.started'], ['offline.success'], ['offline.failed']] },
  { title: '种子下载后清理', cells: [['seed.started'], ['seed.success'], ['seed.failed']] },
  { title: '云端解压', cells: [['cloud.started'], ['cloud.success'], ['cloud.failed']] },
  { title: '本地下载原包', cells: [['local.download_started'], ['local.download_success'], ['local.download_failed']] },
  { title: '本地解压', cells: [['local.started'], ['local.success'], ['local.failed']] },
  { title: '完成后处理', cells: [[], ['post.success', 'post.cleanup_success'], ['post.failed', 'post.cleanup_failed']] },
]
const attention = ['offline.unknown', 'cloud.timeout', 'cloud.password', 'cloud.unknown']
const extras = ['offline.submitted', 'cloud.discovery', 'seed.preview', 'post.success', 'post.failed', 'post.cleanup_success', 'post.cleanup_failed']
function checked(ids: string[]) { return ids.length > 0 && ids.every(id => props.modelValue?.[id]) }
function mixed(ids: string[]) { return !checked(ids) && ids.some(id => props.modelValue?.[id]) }
function set(ids: string[], value: boolean) { emit('update:modelValue', { ...props.modelValue, ...Object.fromEntries(ids.map(id => [id, value])) }) }
const summary = computed(() => props.catalog.filter(x => props.modelValue?.[x.id]).map(x => x.label).join('、') || '不发送任何阶段通知')
</script>

<template>
  <div class="notification-stages">
    <div class="stage-table">
      <div class="stage-row stage-head"><strong>处理环节</strong><span>开始</span><span>成功</span><span>失败</span></div>
      <div v-for="row in rows" :key="row.title" class="stage-row">
        <strong>{{ row.title }}</strong>
        <div v-for="(ids, index) in row.cells" :key="index" class="stage-cell">
          <span class="mobile-label">{{ ['开始', '成功', '失败'][index] }}</span>
          <v-checkbox v-if="ids.length" :model-value="checked(ids)" :indeterminate="mixed(ids)" :disabled="disabled" :aria-label="row.title + ['开始', '成功', '失败'][index]" color="primary" hide-details @update:model-value="set(ids, !!$event)" /><span v-else>—</span>
        </div>
      </div>
    </div>
    <v-checkbox :model-value="checked(attention)" :indeterminate="mixed(attention)" :disabled="disabled" label="需要我处理（密码、超时确认、状态待核对）" hide-details @update:model-value="set(attention, !!$event)" />
    <v-checkbox :model-value="checked(['control.canceled'])" :disabled="disabled" label="主动取消任务时通知" hide-details @update:model-value="set(['control.canceled'], !!$event)" />
    <v-expansion-panels variant="accordion" class="mt-3"><v-expansion-panel title="更多阶段"><v-expansion-panel-text>
      <v-checkbox v-for="stage in catalog.filter(x => extras.includes(x.id) || attention.includes(x.id))" :key="stage.id" :model-value="checked([stage.id])" :disabled="disabled" :label="stage.label" hide-details @update:model-value="set([stage.id], !!$event)" />
    </v-expansion-panel-text></v-expansion-panel></v-expansion-panels>
    <p class="muted mt-3">当前选择：{{ summary }}。多个阶段均开启时会分别通知。</p>
  </div>
</template>

<style scoped>
.notification-stages { margin: 16px 0; }
.stage-row { display: grid; grid-template-columns: minmax(160px, 1fr) repeat(3, 72px); align-items: center; border-bottom: 1px solid rgba(var(--v-theme-on-surface), .1); }
.stage-head { padding: 12px 0; }
.stage-head span { text-align: center; }
.stage-cell { display: flex; align-items: center; justify-content: center; }
.stage-cell :deep(.v-input) { flex: 0 0 auto; }
.mobile-label { display: none; }
@media (max-width: 600px) {
  .stage-head { display: none; }
  .stage-row { grid-template-columns: repeat(3, 1fr); padding: 12px 0; }
  .stage-row > strong { grid-column: 1 / -1; }
  .mobile-label { display: inline; font-size: 12px; }
}
</style>
