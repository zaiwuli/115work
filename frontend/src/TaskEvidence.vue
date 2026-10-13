<script setup lang="ts">
import { ref } from 'vue'
import { Check, Download, RotateCcw, Trash2 } from 'lucide-vue-next'
import { api, bytes, date, stageLabel, stateLabel, textLabel, statusTone } from './api'
import { useSystem } from './store'
const props = defineProps<{ task: any }>()
const emit = defineEmits<{ updated: [] }>()
const system = useSystem()
const confirmName = ref('')
const remoteId = ref('')
const error = ref('')
const busy = ref(false)
async function command(suffix: string, extra: any = {}) {
  busy.value = true
  error.value = ''
  try {
    await api('/api/v1/tasks/' + props.task.id + suffix, {
      ...(suffix === '/commands' ? {} : { confirm_name: confirmName.value }),
      expected_version: props.task.version, ...extra,
    })
    Object.assign(props.task, await api('/api/v1/tasks/' + props.task.id))
    await system.refresh()
    confirmName.value = ''
    emit('updated')
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
</script>

<template>
  <section v-if="task.transfer" class="task-evidence">
    <h3>缓存传输</h3>
    <v-progress-linear :model-value="task.transfer.expected_size ? task.transfer.bytes_done / task.transfer.expected_size * 100 : 0" color="primary" height="5" class="my-3" />
    <div class="transfer-summary"><span>{{ bytes(task.transfer.bytes_done) }} / {{ bytes(task.transfer.expected_size) }}</span><span>{{ bytes(task.transfer.speed) }}/s</span><span v-if="task.transfer.eta !== null">{{ Math.ceil(task.transfer.eta) }} 秒</span></div>
  </section>
  <section v-if="task.steps?.length" class="task-evidence">
    <h3>执行步骤</h3>
    <div v-for="step in task.steps" :key="step.id" class="event-line">
      <div class="flex-content"><strong>{{ stageLabel(step.kind) }}</strong><small>{{ textLabel(step.error || date(step.updated_at)) }}</small></div>
      <span class="state-tag" :class="statusTone(step.state)">{{ stateLabel(step.state) }}</span>
    </div>
  </section>
  <section v-if="task.output.verification || task.output.postprocess_result" class="task-evidence">
    <h3>结果证据</h3>
    <dl class="health-list"><div><dt>验证等级</dt><dd>{{ textLabel(task.output.verification || '-') }}</dd></div><div v-if="task.output.postprocess_result"><dt>后处理</dt><dd>{{ stateLabel(task.output.postprocess_result) }}</dd></div><div><dt>密码候选次数</dt><dd>{{ task.password_attempts }}</dd></div></dl>
  </section>
  <section v-if="system.status?.role !== 'readonly'" class="task-evidence">
    <p v-if="error" role="alert" class="error-text">{{ error }}</p>
    <template v-if="task.state === 'WAITING_APPROVAL' && (task.stage === 'identity_conflict' || task.wait_reason === 'IDENTITY')">
      <h3>内容身份冲突</h3>
      <v-text-field v-model="confirmName" label="确认原文件名" class="mt-3" />
      <v-btn :disabled="busy || confirmName !== task.name" variant="outlined" @click="command('/commands', { action: 'approve' })"><Check :size="17" class="mr-2" />批准独立处理</v-btn>
    </template>
    <template v-if="task.state === 'RECONCILING' && task.stage === 'cloud_export_unknown'">
      <h3>核对云解压操作</h3>
      <div class="form-grid mt-3"><v-text-field v-model="remoteId" label="远端操作 ID" /><v-btn :disabled="busy || !remoteId" @click="command('/reconcile', { remote_id: remoteId })"><Check :size="17" class="mr-2" />核对</v-btn></div>
    </template>
    <template v-if="task.source === '115' && task.state === 'WAITING_APPROVAL' && task.stage === 'local_fallback'">
      <h3>本地解压确认</h3>
      <v-text-field v-model="confirmName" label="确认原文件名" class="mt-3" />
      <v-btn :disabled="busy || confirmName !== task.name || !task.locator?.pickcode" @click="command('/commands', { action: 'approve' })"><Download :size="17" class="mr-2" />确认缓存并本地解压</v-btn>
      <v-btn v-if="task.output.failure_archive_result === 'FAILED'" class="ml-3" :disabled="busy || confirmName !== task.name" @click="command('/failure-archive/reconcile')"><Check :size="17" class="mr-2" />核对失败归档</v-btn>
    </template>
    <template v-if="['SUCCEEDED','FAILED','CANCELED','SKIPPED'].includes(task.state)">
      <h3>处理代次</h3>
      <v-text-field v-model="confirmName" label="确认原文件名" class="mt-3" />
      <div class="import-actions"><v-btn :disabled="busy || confirmName !== task.name" variant="outlined" @click="command('/reprocess')"><RotateCcw :size="17" class="mr-2" />重新处理</v-btn>
        <v-btn v-if="task.state === 'SUCCEEDED' && task.source !== 'legacy'" :disabled="busy || confirmName !== task.name" color="error" variant="outlined" @click="command('/delete-approval')"><Trash2 :size="17" class="mr-2" />批准延迟删除原包</v-btn></div>
    </template>
  </section>
</template>
