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
      ...(suffix === '/commands' ? {} : { confirm_name: suffix === '/delete-approval' ? confirmName.value : props.task.name }),
      expected_version: props.task.version, ...extra,
    })
    Object.assign(props.task, await api('/api/v1/tasks/' + props.task.id))
    confirmName.value = ''
    emit('updated')
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
</script>

<template>
  <section v-if="task.output.flatten_result" class="task-evidence">
    <h3>同名文件夹提层</h3>
    <p :class="task.output.flatten_result === 'FAILED' ? 'error-text' : 'success-text'">{{ task.output.flatten_result === 'FAILED' ? '解压成功，提层失败' : '目录整理完成' }}</p>
    <p v-if="task.output.flatten_error">{{ textLabel(task.output.flatten_error) }}</p>
    <v-btn v-if="task.output.flatten_result === 'FAILED'" :disabled="busy" variant="outlined" @click="command('/postprocess/retry')"><RotateCcw :size="16" class="mr-2" />重试提层</v-btn>
  </section>
  <section v-if="task.output.expected_manifest?.length || task.output.actual_manifest?.length" class="task-evidence">
    <h3>文件清单</h3>
    <details v-if="task.output.expected_manifest?.length"><summary>预计文件（压缩包内容） · {{ task.output.expected_manifest.length }}</summary><div v-for="file in task.output.expected_manifest" :key="file.path" class="event-line"><span class="path-cell">{{ file.path }}</span><small>{{ file.size === null ? '大小未知' : bytes(file.size) }}</small></div></details>
    <details v-if="task.output.actual_manifest?.length"><summary>实际产物 · {{ task.output.actual_manifest.length }}</summary><div v-for="file in task.output.actual_manifest" :key="file.path" class="event-line"><span class="path-cell">{{ file.path }}</span><small>{{ task.output.cleanup_deleted?.includes(file.path) ? '已清理' : bytes(file.size) }}</small></div></details>
  </section>
  <section v-if="task.output.cleanup_result" class="task-evidence">
    <h3>解压产物清理</h3>
    <p :class="task.output.cleanup_result === 'FAILED' ? 'error-text' : 'success-text'">{{ task.output.cleanup_result === 'FAILED' ? '解压成功，清理失败' : '清理完成' }}</p>
    <dl class="health-list"><div><dt>匹配 / 已删除 / 未完成</dt><dd>{{ task.output.cleanup_matched || 0 }} / {{ task.output.cleanup_deleted?.length || 0 }} / {{ task.output.cleanup_failed || 0 }}</dd></div><div v-if="task.output.cleanup_error"><dt>原因</dt><dd>{{ task.output.cleanup_error }}</dd></div></dl>
    <details v-if="task.output.cleanup_deleted?.length"><summary>已清理文件</summary><p v-for="path in task.output.cleanup_deleted" :key="path" class="path-cell">{{ path }}</p></details>
  </section>
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
      <v-btn :disabled="busy" variant="outlined" @click="command('/commands', { action: 'approve' })"><Check :size="17" class="mr-2" />批准独立处理</v-btn>
    </template>
    <template v-if="task.state === 'RECONCILING' && task.stage === 'cloud_export_unknown'">
      <h3>核对云解压操作</h3>
      <div class="form-grid mt-3"><v-text-field v-model="remoteId" label="远端操作 ID" /><v-btn :disabled="busy || !remoteId" @click="command('/reconcile', { remote_id: remoteId })"><Check :size="17" class="mr-2" />核对</v-btn></div>
    </template>
    <template v-if="task.source === '115' && task.state === 'WAITING_APPROVAL' && task.stage === 'local_fallback'">
      <h3>本地解压确认</h3>
      <v-btn :disabled="busy || !task.locator?.pickcode" @click="command('/commands', { action: 'approve' })"><Download :size="17" class="mr-2" />下载原包并本地解压</v-btn>
      <v-btn v-if="task.output.failure_archive_result === 'FAILED'" class="ml-3" :disabled="busy" @click="command('/failure-archive/reconcile')"><Check :size="17" class="mr-2" />核对失败归档</v-btn>
    </template>
    <template v-if="['SUCCEEDED','FAILED','CANCELED','SKIPPED'].includes(task.state)">
      <h3>处理操作</h3>
      <div class="import-actions"><v-btn :disabled="busy" variant="outlined" @click="task.state === 'FAILED' ? command('/commands', { action: 'retry' }) : command('/reprocess')"><RotateCcw :size="17" class="mr-2" />{{ task.state === 'FAILED' ? '重试解压' : '重新处理' }}</v-btn>
        <v-text-field v-if="task.state === 'SUCCEEDED' && task.source !== 'legacy'" v-model="confirmName" label="删除原包前确认文件名" />
        <v-btn v-if="task.state === 'SUCCEEDED' && task.source !== 'legacy'" :disabled="busy || confirmName !== task.name" color="error" variant="outlined" @click="command('/delete-approval')"><Trash2 :size="17" class="mr-2" />批准延迟删除原包</v-btn></div>
    </template>
  </section>
</template>
