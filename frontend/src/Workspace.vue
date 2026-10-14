<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  Activity, Clock3, CheckCircle2, CircleAlert, Download, FileArchive, FileInput,
  Plus, RefreshCw, Search, ArrowUpRight, Trash2, RotateCcw, X, Save, FolderOpen,
  ShieldCheck, ChevronLeft, ChevronRight, Eye, Send, ListFilter, Settings, KeyRound, ChevronDown, Copy,
} from 'lucide-vue-next'
import { api, bytes, date, stageLabel, stateLabel, textLabel, errorLabel, uploadDocument, statusTone as tone } from './api'
import TaskEvidence from './TaskEvidence.vue'
import SettingsNav from './SettingsNav.vue'
import SystemHealth from './SystemHealth.vue'
import { useSystem } from './store'

const route = useRoute()
const system = useSystem()
const view = computed(() => String(route.params.view || 'tasks'))
const settingsSection = computed(() => String(route.query.section || 'cloud'))
const offlineMode = computed(() => view.value === 'tasks' && route.query.type === 'offline')
const offlineBatches = ref<any[]>([])
const canWrite = computed(() => system.status?.role !== 'readonly')
let liveRefresh: ReturnType<typeof setTimeout> | undefined
watch(() => system.status?.updated_at, () => {
  if (!['offline', 'tasks', 'history', 'jobs', 'notifications'].includes(view.value)) return
  clearTimeout(liveRefresh)
  liveRefresh = setTimeout(async () => {
    if (loading.value || !system.autoRefresh) return
    await load(true)
    if (detailOpen.value && detail.value?.id) {
      try { detail.value = await api('/api/v1/tasks/' + detail.value.id) }
      catch { /* Keep the last visible evidence during a transient refresh failure. */ }
    }
  }, 300)
})
onBeforeUnmount(() => clearTimeout(liveRefresh))
const titles: Record<string, string> = {
  passwords: '密码维护',
  overview: '运行总览', tasks: '任务中心', offline: '离线下载', documents: '文档导入',
  sources: '来源管理', mappings: '目录映射', jobs: '自动任务', history: '处理历史',
  notifications: '通知记录', settings: '系统设置', maintenance: '数据维护',
}
const rows = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const stateFilter = ref('')
const search = ref('')
const loading = ref(false)
const message = ref('')
const error = ref('')
const notificationKey = ref('')
const savingSettings = ref(false)
const saveFeedback = ref('')
const saveSucceeded = ref(false)
const saveNoticeOpen = ref(false)
const linkText = ref('')
const shortcutEndpoint = window.location.origin + '/api/v1/offline/import'
async function copyShortcutEndpoint() {
  try {
    await navigator.clipboard.writeText(shortcutEndpoint)
    message.value = '快捷指令接口地址已复制'
  } catch { error.value = '浏览器不允许复制，请选择接口地址手动复制' }
}
const autoExtract = ref(true)
const upload = ref<File | null>(null)
const detail = ref<any>(null)
const detailOpen = ref(false)
const sourceOpen = ref(false)
const editingSource = ref<any>(null)
const mappingOpen = ref(false)
const settings = ref<any>({})
const savedPasswords = ref<string[]>([])
async function refreshPasswords() {
  settings.value = await api('/api/v1/settings')
  savedPasswords.value = (await api('/api/v1/passwords')).items
}
const newPassword = ref('')
const sources = ref<any[]>([])
const connections = ref<any[]>([])
const documentUpload = ref<File | null>(null)
const sourceExcludes = ref('')
const newSource = ref({ name: '', kind: 'local', path: '', cid: '', extract_cid: '', output_path: '', interval: 60, mount_marker: '', connection_id: '', virtual_path: '/' })
const newMapping = ref({ virtual_root: '', container_root: '', host_path: '', root_cid: '', connection: '' })
const diagnosisPath = ref('')
const diagnosis = ref<any>(null)
const confirmation = ref<{ title: string, endpoint: string, body?: unknown, method?: string } | null>(null)
let searchTimer: ReturnType<typeof setTimeout> | undefined

const statistics = computed(() => offlineMode.value ? [
  { label: '进行中', state: 'ACTIVE', value: allOfflineItems.value.filter(x => !['success','failed','submit_failed','canceled'].includes(x.status)).length, icon: Activity, color: 'teal' },
  { label: '等待提交', state: 'queued', value: allOfflineItems.value.filter(x => x.status === 'queued').length, icon: Clock3, color: 'amber' },
  { label: '离线失败', state: 'FAILURES', value: allOfflineItems.value.filter(x => ['failed','submit_failed'].includes(x.status)).length, icon: CircleAlert, color: 'red' },
  { label: '离线成功', state: 'success', value: allOfflineItems.value.filter(x => x.status === 'success').length, icon: CheckCircle2, color: 'green' },
] : [
  { label: '进行中', state: 'ACTIVE', value: system.status?.totals.active || 0, icon: Activity, color: 'teal' },
  { label: '等待批准', state: 'WAITING_APPROVAL', value: system.status?.totals.approvals || 0, icon: Clock3, color: 'amber' },
  { label: '处理失败', state: 'FAILED', value: system.status?.totals.failed || 0, icon: CircleAlert, color: 'red' },
  { label: '已完成', state: 'SUCCEEDED', value: system.status?.totals.finished || 0, icon: CheckCircle2, color: 'green' },
])
const allOfflineItems = computed(() => Array.from(new Map(offlineBatches.value.flatMap(batch =>
  batch.tasks.map((task: any) => ({ ...task, import_date: batch.target_date, import_cid: batch.target_cid })),
).map((task: any) => [task.id, task])).values()) as any[])
const offlineItems = computed(() => allOfflineItems.value.filter(task =>
  (!search.value || task.name.toLowerCase().includes(search.value.toLowerCase())) &&
  (!stateFilter.value || (stateFilter.value === 'ACTIVE'
    ? !['success','failed','submit_failed','canceled'].includes(task.status)
    : stateFilter.value === 'FAILURES' ? ['failed','submit_failed'].includes(task.status)
    : task.status === stateFilter.value)),
))
const offlineStateOptions = [
  { title: '全部状态', value: '' }, { title: '进行中', value: 'ACTIVE' },
  { title: '等待提交', value: 'queued' }, { title: '提交中', value: 'submitting' },
  { title: '已提交', value: 'submitted' }, { title: '下载中', value: 'downloading' },
  { title: '离线成功', value: 'success' }, { title: '离线失败 / 提交失败', value: 'FAILURES' },
  { title: '待核对', value: 'unknown' }, { title: '提交结果待核对', value: 'submission_unknown' },
  { title: '已取消', value: 'canceled' },
]
const progressTasks = computed(() => (system.status?.tasks || []).slice(0, 8))
const stateOptions = [
  { title: '全部状态', value: '' }, { title: '等待处理', value: 'READY' },
  { title: '进行中（所有未结束任务）', value: 'ACTIVE' },
  { title: '处理中', value: 'RUNNING' }, { title: '等待远端', value: 'WAITING_EXTERNAL' },
  { title: '等待批准', value: 'WAITING_APPROVAL' }, { title: '等待重试', value: 'WAITING_RETRY' },
  { title: '完成', value: 'SUCCEEDED' }, { title: '失败', value: 'FAILED' },
  { title: '待核对', value: 'RECONCILING' },
]
let loadGeneration = 0
async function load(silent = false) {
  const generation = ++loadGeneration
  const requestedView = view.value
  const requestedOffline = offlineMode.value
  const current = () => generation === loadGeneration && requestedView === view.value && requestedOffline === offlineMode.value
  if (!silent) { loading.value = true; error.value = '' }
  try {
    if (view.value === 'overview') { await system.refresh(); return }
    if (view.value === 'passwords') { await refreshPasswords(); return }
    if (view.value === 'settings') { savedPasswords.value = []; settings.value = await api('/api/v1/settings'); return }
    if (view.value === 'maintenance') return
    if (offlineMode.value) {
      const result = await api('/api/v1/offline')
      if (current() && JSON.stringify(offlineBatches.value) !== JSON.stringify(result.batches)) offlineBatches.value = result.batches
      return
    }
    if (view.value === 'offline') return
    if (view.value === 'tasks' || view.value === 'history') {
      const result = await api(`/api/v1/tasks?page=${page.value}&page_size=30&terminal=${view.value === 'history'}&state=${encodeURIComponent(stateFilter.value)}&q=${encodeURIComponent(search.value)}`)
      if (!current()) return
      if (JSON.stringify(rows.value) !== JSON.stringify(result.items)) rows.value = result.items
      total.value = result.total
    } else if (view.value === 'offline') rows.value = (await api('/api/v1/offline')).batches
    else rows.value = (await api('/api/v1/' + view.value)).items
    if (['sources', 'mappings'].includes(view.value) && system.status?.role !== 'readonly') connections.value = (await api('/api/v1/connections')).items
  } catch (cause) { if (!silent && current()) error.value = (cause as Error).message }
  finally { if (current()) loading.value = false }
}
async function action(endpoint: string, body: unknown = {}, success = '操作已提交') {
  loading.value = true
  try {
    await api(endpoint, body)
    message.value = success
    await load()
    return true
  } catch (cause) { error.value = (cause as Error).message; return false }
  finally { loading.value = false }
}
async function importLinks() {
  try {
    if (upload.value && !upload.value.name.toLowerCase().endsWith('.txt')) {
      if (linkText.value.trim()) {
        error.value = '文档导入与粘贴链接请分别提交，避免重复登记'
        return
      }
      await uploadDocument(upload.value)
      message.value = '文件已登记，解析完成后可在任务中心查看离线任务'
      upload.value = null
      return
    }
    const text = linkText.value + (upload.value ? '\n' + await upload.value.text() : '')
    const result = await api('/api/115/offline/import', { text, auto_extract: autoExtract.value })
    message.value = `已登记 ${result.queued} 条，重复 ${result.duplicates} 条，无效 ${result.invalid} 条`
    linkText.value = ''
    upload.value = null
    await load()
  } catch (cause) { error.value = (cause as Error).message }
}
async function showTask(task: any) {
  try {
    detail.value = await api('/api/v1/tasks/' + task.id)
    detailOpen.value = true
  } catch (cause) { error.value = (cause as Error).message }
}
async function saveSource() {
  const value = newSource.value
  const fields = value.kind === '115' ? { cid: value.cid, extract_cid: value.extract_cid } : {
    path: value.path, ...(value.kind !== 'inbox' ? { output_path: value.output_path } : {}),
  }
  const config = { ...(editingSource.value?.kind === value.kind ? editingSource.value.config : {}), ...fields }
  if (['local', 'inbox'].includes(value.kind)) {
    Object.assign(config, { exclude_paths: sourceExcludes.value.split('\n').map(x => x.trim()).filter(Boolean) })
  }
  try {
    await api('/api/v1/sources' + (editingSource.value ? '/' + editingSource.value.id : ''), {
      name: value.name, kind: value.kind, config, interval: value.interval,
      enabled: editingSource.value?.enabled ?? true,
      ...(editingSource.value ? { expected_revision: editingSource.value.revision } : {}),
    }, editingSource.value ? 'PATCH' : 'POST')
    sourceOpen.value = false
    await load()
  } catch (cause) { error.value = (cause as Error).message }
}
function openSource(source?: any) {
  editingSource.value = source || null
  sourceExcludes.value = (source?.config.exclude_paths || []).join('\n')
  newSource.value = {
    name: source?.name || '', kind: source?.kind || 'local', path: source?.config.path || '',
    cid: source?.config.cid || '', interval: source?.interval ?? 60,
    extract_cid: source?.config.extract_cid || '', output_path: source?.config.output_path || '',
    mount_marker: source?.config.mount_marker || '', connection_id: source?.config.connection_id || '',
    virtual_path: source?.config.virtual_path || '/',
  }
  sourceOpen.value = true
}
async function sendDocument() {
  if (!documentUpload.value) return
  try {
    await uploadDocument(documentUpload.value)
    message.value = '文档已登记'
    documentUpload.value = null
    await load()
  } catch (cause) { error.value = (cause as Error).message }
}
async function saveSettings() {
  if (savingSettings.value) return
  savingSettings.value = true
  saveFeedback.value = ''
  error.value = ''
  const values: any = {}
  for (const key of ['local_extract_concurrency', 'life_monitor_enabled', 'cloud_extract_by_date', 'cloud_retry_seconds',
    'cloud_cooldown_seconds', 'offline_fallback_seconds', 'notification_offline',
    'notification_cloud', 'notification_local', 'notification_webhook',
    'notification_enabled', 'notification_provider', 'notification_template',
    'notification_discovery', 'notification_cache', 'notification_extract',
    'notification_complete', 'notification_cleanup']) values[key] = settings.value[key]
  let settingsSaved = false
  try {
    await api('/api/settings', values)
    settingsSaved = true
    const hasKey = !!notificationKey.value
    if (hasKey) {
      await api('/api/notification', { api_key: notificationKey.value })
      notificationKey.value = ''
    }
    settings.value = await api('/api/v1/settings')
    saveSucceeded.value = true
    saveFeedback.value = hasKey ? '通知设置和 MS 密钥均已保存' : '设置已保存'
  } catch (cause) {
    saveSucceeded.value = false
    saveFeedback.value = (settingsSaved ? '设置已保存，但密钥保存或状态读取失败：' : '保存失败：') + (cause as Error).message
    error.value = saveFeedback.value
  } finally {
    savingSettings.value = false
    saveNoticeOpen.value = true
  }
}
async function toggleSource(source: any, enabled: boolean) {
  try { await api('/api/v1/sources/' + source.id, { enabled }, 'PATCH'); await load() }
  catch (cause) { error.value = (cause as Error).message }
}
async function confirm() {
  if (!confirmation.value) return
  const value = confirmation.value
  confirmation.value = null
  try {
    await api(value.endpoint, value.body, value.method)
    message.value = '操作成功'
    await load()
  } catch (cause) { error.value = (cause as Error).message }
}
watch([view, offlineMode], () => { rows.value = []; offlineBatches.value = []; stateFilter.value = ''; search.value = ''; page.value = 1; message.value = ''; void load() }, { immediate: true })
watch(page, () => void load())
watch(() => system.refreshVersion, () => void load())
watch(stateFilter, () => { if (page.value !== 1) page.value = 1; else void load() })
watch(search, () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { page.value = 1; void load() }, 250) })
</script>

<template>
  <section v-mobile-tables class="workspace">
    <header class="page-heading">
      <div><div class="eyebrow">115work</div><h1>{{ titles[view] || '运行总览' }}</h1></div>
      <div class="heading-actions">
        <v-btn v-if="view === 'sources' && canWrite" color="primary" @click="openSource()"><Plus :size="17" class="mr-2" />添加来源</v-btn>
        <v-btn v-if="canWrite && offlineMode" variant="outlined" :disabled="loading" @click="action('/api/115/offline/refresh')"><RefreshCw :size="16" class="mr-2" />查询离线</v-btn>
        <v-btn v-if="canWrite && view === 'tasks' && !offlineMode" variant="outlined" :disabled="loading" @click="action('/api/v1/system/refresh')"><RefreshCw :size="16" class="mr-2" />手动扫描</v-btn>
        <v-btn v-if="canWrite && view === 'tasks' && !offlineMode" variant="outlined" :disabled="loading" @click="action('/api/v1/cloud/monitor/check', {}, '已加入生活事件检查队列')"><Activity :size="16" class="mr-2" />检查生活事件</v-btn>
      </div>
    </header>
    <p v-if="error" class="error-text alert" role="alert">{{ error }}</p>
    <p v-if="message" class="success-text alert" role="status">{{ message }}</p>
    <div class="loading-slot"><v-progress-linear :style="{ visibility: loading ? 'visible' : 'hidden' }" :indeterminate="loading" color="primary" height="2" /></div>
    <nav v-if="view === 'tasks'" class="section-nav" aria-label="任务类型">
      <RouterLink to="/tasks" :class="{ active: !offlineMode }">解压任务</RouterLink>
      <RouterLink to="/tasks?type=offline" :class="{ active: offlineMode }">离线任务</RouterLink>
    </nav>

    <section v-if="view === 'tasks'" aria-label="运行总览">
      <header class="section-heading"><h2>运行总览</h2></header>
      <div class="stat-grid">
        <button v-for="stat in statistics" :key="stat.label" class="stat-item"
          :class="{ selected: stateFilter === stat.state }" :aria-pressed="stateFilter === stat.state"
          :aria-label="'查看' + stat.label + '任务'" @click="search = ''; stateFilter = stat.state">
          <div><span>{{ stat.label }}</span><strong>{{ stat.value }}</strong></div>
          <component :is="stat.icon" :size="26" :class="stat.color" />
        </button>
      </div>
    </section>

    <template v-if="(view === 'tasks' && !offlineMode) || view === 'history'">
      <div class="filter-bar"><Search :size="18" class="muted" /><v-text-field v-model="search" label="搜索文件名" class="search-field" /><v-select v-model="stateFilter" :items="stateOptions" label="状态" class="filter-select" /></div>
      <header class="section-heading task-list-heading"><h2>解压任务</h2></header>
      <div class="table-wrap mobile-task-list">
        <table><thead><tr><th>文件 / 来源</th><th>当前阶段</th><th>状态</th><th>大小</th><th>尝试</th><th>操作</th></tr></thead><tbody>
          <tr v-for="task in rows" :key="task.id">
            <td><button class="file-link" @click="showTask(task)"><FileArchive :size="18" /><strong>{{ task.name }}</strong></button><small>{{ stateLabel(task.source) }} · {{ date(task.created_at) }}</small></td>
            <td>{{ stageLabel(task.stage) }}<small v-if="task.error" class="error-text">{{ textLabel(task.error) }}</small></td>
            <td><span class="state-tag" :class="tone(task.state)">{{ stateLabel(task.state) }}</span><small v-if="task.output.cleanup_result === 'FAILED'" class="error-text">解压成功，清理失败</small><small v-if="task.output.flatten_result === 'FAILED'" class="error-text">解压成功，提层失败</small></td><td class="nowrap">{{ bytes(task.size) }}</td><td>{{ task.attempts }}</td>
            <td><div class="row-actions">
              <button title="详情" aria-label="任务详情" @click="showTask(task)"><Eye :size="17" /></button>
              <template v-if="canWrite">
              <button v-if="task.state === 'FAILED'" title="重试" aria-label="重试任务" @click="action('/api/v1/tasks/' + task.id + '/commands', { action: 'retry' })"><RotateCcw :size="17" /></button>
              <button v-if="task.stage === 'cloud_password'" title="重试密码" aria-label="重试密码" @click="action('/api/v1/tasks/' + task.id + '/commands', { action: 'approve' })"><CheckCircle2 :size="17" /></button>
              <button v-if="task.stage === 'local_fallback' && task.state === 'WAITING_APPROVAL'" title="批准本地兜底" aria-label="批准本地兜底" @click="confirmation = { title: '批准下载原包并进行本地解压？', endpoint: '/api/v1/tasks/' + task.id + '/commands', body: { action: 'approve', expected_version: task.version } }"><Download :size="17" /></button>
              <button v-if="task.output.postprocess_result === 'FAILED'" :title="task.output.cleanup_result === 'FAILED' ? '重试清理' : '重试后处理'" aria-label="重试后处理" @click="action('/api/v1/tasks/' + task.id + '/postprocess/retry')"><RotateCcw :size="17" /></button>
              <button v-if="!['SUCCEEDED', 'FAILED', 'CANCELED', 'SKIPPED'].includes(task.state)" title="请求取消" aria-label="请求取消任务" @click="confirmation = { title: '请求取消此任务？', endpoint: '/api/v1/tasks/' + task.id + '/commands', body: { action: 'cancel' } }"><X :size="17" /></button>
              <button v-if="['SUCCEEDED', 'FAILED', 'CANCELED', 'SKIPPED'].includes(task.state)" title="删除任务" aria-label="删除任务记录" @click="confirmation = { title: '彻底删除此任务及去重记录？原包和解压结果保留，下次发现可能重新解压。', endpoint: '/api/v1/tasks/' + task.id, method: 'DELETE' }"><Trash2 :size="17" /></button>
              </template>
            </div></td>
          </tr>
        </tbody></table>
        <div v-if="!rows.length" class="empty"><ListFilter :size="32" /><span>暂无匹配的任务</span></div>
      </div>
      <div class="pagination"><span>共 {{ total }} 条</span><v-btn icon variant="text" :disabled="page <= 1" aria-label="上一页" @click="page--"><ChevronLeft :size="18" /></v-btn><span>{{ page }}</span><v-btn icon variant="text" :disabled="page * 30 >= total" aria-label="下一页" @click="page++"><ChevronRight :size="18" /></v-btn></div>
    </template>

    <template v-else-if="view === 'offline'">
      <section class="import-section">
        <v-textarea v-model="linkText" label="ED2K / 磁力 / HTTP / FTP" rows="4" />
        <div class="import-actions">
          <label class="file-upload"><FileInput :size="17" />{{ upload?.name || '选择文件' }}<input type="file" accept=".txt,.md,.csv,.json,.pdf,.docx" @change="upload = ($event.target as HTMLInputElement).files?.[0] || null" /></label>
          <v-checkbox v-if="!upload || upload.name.toLowerCase().endsWith('.txt')" v-model="autoExtract" label="自动解压" hide-details density="compact" />
          <v-spacer /><v-btn color="primary" :disabled="(!linkText.trim() && !upload) || loading" @click="importLinks"><Download :size="17" class="mr-2" />导入链接</v-btn>
        </div>
      </section>
    </template>
    <template v-else-if="offlineMode">
      <div class="filter-bar"><Search :size="18" class="muted" /><v-text-field v-model="search" label="搜索文件名" class="search-field" /><v-select v-model="stateFilter" :items="offlineStateOptions" label="状态" class="filter-select" /></div>
      <header class="section-heading task-list-heading"><h2>离线记录</h2><v-btn variant="text" :disabled="loading" @click="confirmation = { title: '重试全部明确失败的离线任务？', endpoint: '/api/115/offline/retry-all', body: {} }"><RotateCcw :size="16" class="mr-2" />重试失败项</v-btn></header>
      <div class="table-wrap mobile-task-list offline-task-list"><table><thead><tr><th>文件</th><th>日期 / 实际 CID</th><th>离线状态</th><th>处理阶段</th><th>操作</th></tr></thead><tbody>
        <tr v-for="task in offlineItems" :key="task.id + task.import_date">
          <td><strong>{{ task.name }}</strong><small>{{ task.kind.toUpperCase() }} · {{ bytes(task.size) }}</small><small class="mono">{{ task.hash_value }}</small></td>
          <td>{{ task.target_date }}<small class="mono">CID {{ task.target_cid || '待创建' }}</small><small v-if="task.import_date !== task.target_date">导入批次 {{ task.import_date }}</small></td>
          <td><span class="state-tag" :class="tone(task.status)">{{ stateLabel(task.status) }}</span><small class="error-text" v-if="task.error">{{ textLabel(task.error) }}</small></td>
          <td>{{ stageLabel(task.pipeline_status) }}<small v-if="task.artifact_count">关联产物 {{ task.artifact_count }} 项</small><small>查询 {{ task.check_count }} 次 · 重试 {{ task.retry_count }} 次</small></td>
          <td><div class="row-actions"><button v-if="['failed', 'submit_failed'].includes(task.status)" aria-label="重试离线任务" title="重试" @click="action('/api/115/offline/retry', { task_id: task.id })"><RotateCcw :size="17" /></button><button v-for="archive in task.archives" :key="archive.id" title="关联任务" aria-label="查看关联任务" @click="showTask(archive)"><ArrowUpRight :size="17" /></button><button v-if="canWrite && !['submitting','submission_unknown'].includes(task.status)" title="删除离线记录" aria-label="删除离线记录" @click="confirmation = { title: '彻底删除本工具的离线记录并停止跟踪？不会取消115下载，云文件及关联解压任务保留。', endpoint: '/api/v1/offline/' + task.id, method: 'DELETE' }"><Trash2 :size="17" /></button></div></td>
        </tr>
      </tbody></table><div v-if="!offlineItems.length" class="empty"><Download :size="32" /><span>暂无离线记录</span></div></div>
    </template>

    <template v-else-if="view === 'sources'">
      <div class="table-wrap"><table><thead><tr><th>来源</th><th>类型</th><th>目录</th><th>扫描间隔</th><th>启用</th><th>操作</th></tr></thead><tbody>
        <tr v-for="source in rows" :key="source.id"><td><strong>{{ textLabel(source.name) }}</strong><small>版本 {{ source.revision }}</small></td><td>{{ stateLabel(source.kind) }}</td><td class="path-cell">{{ source.config.path || source.config.virtual_path || ('CID ' + source.config.cid) }}</td><td>{{ source.kind === '115' ? '生活事件 / 手动' : source.interval ? source.interval + ' 秒' : '手动' }}<small v-if="source.kind !== '115'">{{ date(source.next_run) }}</small></td><td><v-switch :model-value="source.enabled" :disabled="!canWrite" hide-details color="primary" density="compact" :aria-label="'启用' + source.name" @update:model-value="toggleSource(source, !!$event)" /></td><td><div v-if="canWrite" class="row-actions"><button title="编辑来源" aria-label="编辑来源" @click="openSource(source)"><Settings :size="17" /></button><button title="删除来源" aria-label="删除来源" @click="confirmation = { title: '删除此来源？停止监测该目录，保留已有任务、处理规则及所有真实文件。', endpoint: '/api/v1/sources/' + source.id, method: 'DELETE' }"><Trash2 :size="17" /></button></div></td></tr>
      </tbody></table><div v-if="!rows.length" class="empty"><FolderOpen :size="32" /><span>暂无来源</span></div></div>
    </template>

    <template v-else-if="view === 'jobs'">
      <div class="table-wrap"><table><thead><tr><th>工作类型 / 合并键</th><th>状态</th><th>下次执行</th><th>领取次数</th><th>错误</th></tr></thead><tbody>
        <tr v-for="job in rows" :key="job.id"><td><strong>{{ stageLabel(job.kind) }}</strong><small class="mono">{{ job.key }}</small></td><td><span class="state-tag" :class="tone(job.state)">{{ stateLabel(job.state) }}</span><small v-if="job.dirty">结束后补查</small></td><td>{{ date(job.due) }}</td><td>{{ job.dispatch_count }}</td><td class="error-text">{{ textLabel(job.error || '-') }}</td></tr>
      </tbody></table><div v-if="!rows.length" class="empty"><Clock3 :size="32" /><span>暂无调度记录</span></div></div>
    </template>

    <template v-else-if="view === 'documents'">
      <div v-if="canWrite" class="import-actions mb-6"><label class="file-upload"><FileInput :size="17" />{{ documentUpload?.name || '选择文档' }}<input type="file" accept=".txt,.md,.csv,.json,.pdf,.docx" @change="documentUpload = ($event.target as HTMLInputElement).files?.[0] || null" /></label><v-btn color="primary" :disabled="!documentUpload || loading" @click="sendDocument"><FileInput :size="17" class="mr-2" />导入文档</v-btn></div>
      <div class="table-wrap"><table><thead><tr><th>文档</th><th>状态</th><th>统计</th><th>错误</th><th>操作</th></tr></thead><tbody><tr v-for="document in rows" :key="document.digest"><td class="path-cell"><strong>{{ document.path }}</strong><small class="mono">{{ document.digest.slice(0, 24) }}</small></td><td><span class="state-tag" :class="tone(document.state)">{{ stateLabel(document.state) }}</span></td><td>登记 {{ document.statistics.queued || 0 }} · 重复 {{ document.statistics.duplicates || 0 }} · 无效 {{ document.statistics.invalid || 0 }}<small>{{ document.statistics.encoding || '-' }}</small></td><td class="error-text">{{ textLabel(document.error || '-') }}</td><td><button v-if="canWrite && document.state === 'FAILED'" title="重试文档" aria-label="重试文档" @click="action('/api/v1/documents/' + document.digest + '/retry')"><RotateCcw :size="17" /></button></td></tr></tbody></table><div v-if="!rows.length" class="empty"><FileInput :size="32" /><span>暂无文档导入记录</span></div></div>
    </template>

    <template v-else-if="view === 'notifications'">
      <div class="table-wrap"><table><thead><tr><th>事件</th><th>分类</th><th>发送状态</th><th>尝试</th><th>时间</th></tr></thead><tbody><tr v-for="notice in rows" :key="notice.id"><td>{{ stageLabel(notice.event.stage) }}<small>{{ textLabel(notice.event.message) }}</small></td><td>{{ textLabel(notice.category) }}</td><td><span class="state-tag" :class="tone(notice.status)">{{ stateLabel(notice.status) }}</span><small class="error-text">{{ textLabel(notice.error) }}</small></td><td>{{ notice.attempts }}</td><td>{{ date(notice.event.created_at) }}</td></tr></tbody></table><div v-if="!rows.length" class="empty"><Send :size="32" /><span>暂无通知</span></div></div>
    </template>

    <template v-else-if="view === 'settings'">
      <SettingsNav />
      <SystemHealth v-if="settingsSection === 'status'" />
      <section v-if="settingsSection === 'status'" class="settings-section"><h2>快捷指令接口</h2><div class="import-actions"><v-text-field :model-value="shortcutEndpoint" label="离线提交地址（POST）" readonly /><v-btn icon variant="text" title="复制接口地址" aria-label="复制接口地址" @click="copyShortcutEndpoint"><Copy :size="18" /></v-btn></div></section>
      <section v-if="settingsSection === 'status'" class="settings-section"><h2>界面刷新</h2><v-switch :model-value="system.autoRefresh" label="自动刷新页面数据" color="primary" hide-details @update:model-value="system.setAutoRefresh(!!$event)" /></section>
      <section v-if="settingsSection === 'cloud'" class="settings-section"><h2>115 云根目录</h2><div class="form-grid"><v-text-field v-model="settings.cloud_root_cid" label="根目录 CID" /><v-btn :disabled="loading || !settings.cloud_root_cid" color="primary" @click="action('/api/v1/cloud/root', { cid: settings.cloud_root_cid }, '云目录已初始化')"><FolderOpen :size="17" class="mr-2" />初始化目录</v-btn></div><div class="form-grid"><v-text-field :model-value="settings.offline_root_cid" label="离线目录 CID" readonly /><v-text-field :model-value="settings.cloud_output_cid" label="解压目录 CID" readonly /><v-text-field :model-value="settings.cloud_archive_cid" label="归档目录 CID" readonly /><v-text-field :model-value="settings.cloud_failed_cid" label="失败目录 CID" readonly /></div><v-switch v-model="settings.cloud_extract_by_date" label="云解压输出按日期建目录" color="primary" hide-details /></section>
      <section v-if="settingsSection === 'cloud'" class="settings-section"><h2>云目录监测</h2><div class="import-actions"><v-switch v-model="settings.life_monitor_enabled" label="启用生活事件监测（每30分钟）" color="primary" hide-details /><v-btn variant="outlined" to="/sources"><FolderOpen :size="17" class="mr-2" />监测目录管理</v-btn></div></section>
      <section v-if="settingsSection === 'scheduler'" class="settings-section"><h2>调度节奏</h2><div class="form-grid"><v-select v-model="settings.local_extract_concurrency" :items="[1, 2, 3, 4]" label="本地同时解压数量" /><v-text-field v-model.number="settings.cloud_retry_seconds" type="number" min="15" label="云解压重试间隔 / 秒" /><v-text-field v-model.number="settings.cloud_cooldown_seconds" type="number" min="15" label="成功冷却 / 秒" /><v-text-field v-model.number="settings.offline_fallback_seconds" type="number" min="15" label="离线兜底查询 / 秒" /></div></section>
      <section v-if="settingsSection === 'notifications'" class="settings-section notification-form"><h2>通知</h2>
        <v-switch v-model="settings.notification_enabled" label="启用通知" color="primary" hide-details />
        <v-select v-model="settings.notification_provider" label="通知方式" :items="[{ title: 'MS 通知', value: 'ms' }, { title: '常规 Webhook', value: 'webhook' }]" />
        <div class="toggle-row"><v-switch v-model="settings.notification_offline" label="离线通知" color="primary" hide-details /><v-switch v-model="settings.notification_cloud" label="云解压通知" color="primary" hide-details /><v-switch v-model="settings.notification_local" label="本地处理通知" color="primary" hide-details /></div>
        <div class="toggle-row"><v-checkbox v-for="stage in [{ key: 'discovery', title: '发现压缩包' }, { key: 'cache', title: '开始缓存' }, { key: 'extract', title: '开始解压' }, { key: 'complete', title: '完成 / 离线结果' }, { key: 'cleanup', title: '原包处理完成' }]" :key="stage.key" v-model="settings['notification_' + stage.key]" :label="stage.title" hide-details /></div>
        <v-text-field v-model="settings.notification_webhook" label="通知服务地址" />
        <v-text-field v-if="settings.notification_provider === 'ms'" v-model="notificationKey" type="password" autocomplete="new-password" :label="settings.notification_api_key_configured ? 'MS 通知密钥（已配置）' : 'MS 通知密钥'" />
        <v-textarea v-model="settings.notification_template" label="通知模板" rows="5" placeholder="留空使用原工具默认模板" />
        <v-btn variant="text" class="mt-3" @click="action('/api/notification', { api_key: '' }, 'MS 密钥已清除')"><Trash2 :size="16" class="mr-2" />清除密钥</v-btn>
        <v-btn variant="text" class="mt-3" @click="action('/api/notification/test')"><Send :size="16" class="mr-2" />测试通知</v-btn>
      </section>
      <div v-if="['cloud', 'scheduler', 'notifications'].includes(settingsSection)" class="save-row"><v-btn color="primary" :disabled="loading || savingSettings" :loading="savingSettings" @click="saveSettings"><Save :size="17" class="mr-2" />{{ savingSettings ? '保存中' : '保存设置' }}</v-btn><p v-if="saveFeedback" :class="saveSucceeded ? 'success-text' : 'error-text'" role="status">{{ saveFeedback }}</p></div>
      <section v-if="settingsSection === 'passwords'" class="settings-section"><h2>压缩包密码 <span class="muted">({{ settings.password_count || 0 }})</span></h2><div class="form-grid"><v-text-field v-model="newPassword" label="新增密码" type="password" autocomplete="off" /><v-btn variant="outlined" :disabled="!newPassword" @click="action('/api/passwords', { action: 'add', password: newPassword }).then(() => newPassword = '')"><Plus :size="16" class="mr-2" />添加</v-btn></div><div class="password-list"><div v-for="index in (settings.password_count || 0)" :key="index"><span>密码 {{ index }} · ********</span><button :aria-label="'移除密码' + index" title="移除密码" @click="action('/api/passwords', { action: 'remove', index: index - 1 })"><Trash2 :size="16" /></button></div></div></section>
    </template>

    <template v-else-if="view === 'maintenance'">
      <div class="maintenance-list">
        <div><div><h2>一致性备份</h2></div><v-btn variant="outlined" @click="action('/api/v1/maintenance/backup', {}, '备份已保存到 config/backups')"><Save :size="17" class="mr-2" />创建备份</v-btn></div>
        <div><div><h2>回收无引用缓存</h2><p>活动任务与审批待用缓存保留</p></div><v-btn variant="outlined" @click="confirmation = { title: '回收已完成或已取消任务的缓存？', endpoint: '/api/maintenance', body: { action: 'clear_cache' } }"><Trash2 :size="17" class="mr-2" />回收缓存</v-btn></div>
        <div><div><h2>隐藏历史展示</h2><p>保留成功、失败与防重复身份</p></div><v-btn variant="outlined" @click="confirmation = { title: '隐藏终态历史记录？防重复身份会保留。', endpoint: '/api/maintenance', body: { action: 'hide_history' } }">隐藏记录</v-btn></div>
        <div><div><h2>隐藏离线记录</h2><p>保留链接身份，不更改 115 任务或文件</p></div><v-btn variant="outlined" @click="confirmation = { title: '隐藏离线记录？链接防重复会保留。', endpoint: '/api/115/offline/clear', body: { scope: 'all' } }">隐藏记录</v-btn></div>
        <div><div><h2>重置离线本地记录</h2><p>需暂停且无活动离线执行；允许相同链接再次导入</p></div><v-btn color="error" variant="outlined" @click="confirmation = { title: '重置离线本地记录？115 中的文件不受影响。', endpoint: '/api/115/offline/clear', body: { action: 'reset' } }">重置离线记录</v-btn></div>
        <div><div><h2>清除已结束任务的去重记录</h2><p>需暂停；再次扫描时原包可能重新解压，不删除原包或输出文件</p></div><v-btn color="error" variant="outlined" @click="confirmation = { title: '清除已结束任务的去重记录？再次扫描可能重复解压原包。', endpoint: '/api/maintenance', body: { action: 'reset_identities' } }">清除解压去重记录</v-btn></div>
      </div>
    </template>

    <v-dialog v-model="detailOpen" max-width="760"><v-card class="detail-card"><header class="dialog-heading"><h2>{{ detail?.name }}</h2><v-btn icon variant="text" aria-label="关闭详情" @click="detailOpen = false"><X :size="20" /></v-btn></header><v-card-text v-if="detail"><dl class="health-list"><div><dt>状态</dt><dd>{{ stateLabel(detail.state) }}</dd></div><div><dt>阶段</dt><dd>{{ stageLabel(detail.stage) }}</dd></div><div><dt>来源</dt><dd>{{ stateLabel(detail.source) }}</dd></div><div><dt>文件大小</dt><dd>{{ bytes(detail.size) }}</dd></div><div><dt>业务尝试</dt><dd>{{ detail.attempts }}</dd></div><div><dt>输出</dt><dd class="path-cell">{{ detail.output.path || detail.output.cid || '-' }}</dd></div><div v-if="detail.error"><dt>错误</dt><dd class="error-text">{{ errorLabel(detail.error) }}</dd></div></dl><TaskEvidence :task="detail" /><h3 class="mt-5 mb-3">任务时间线</h3><div v-for="event in detail.events" :key="event.id" class="timeline-row"><span class="event-dot" /><div><strong>{{ stageLabel(event.stage) }}</strong><p>{{ textLabel(event.message) }}</p><small>{{ date(event.created_at) }}</small></div></div></v-card-text></v-card></v-dialog>
    <v-dialog v-model="sourceOpen" max-width="560"><v-card>
      <header class="dialog-heading"><h2>{{ editingSource ? '编辑来源' : '添加来源' }}</h2><v-btn icon variant="text" aria-label="关闭" @click="sourceOpen = false"><X :size="20" /></v-btn></header>
      <v-card-text><v-form @submit.prevent="saveSource" class="dialog-form">
        <v-text-field v-model="newSource.name" label="来源名称" />
        <v-select v-model="newSource.kind" :items="[{ title: 'NAS / 本地压缩包', value: 'local' }, { title: '链接收件箱', value: 'inbox' }, { title: '115 云目录', value: '115' }]" label="来源类型" />
        <template v-if="newSource.kind === '115'"><v-text-field v-model="newSource.cid" label="目录 CID" /><v-text-field v-model="newSource.extract_cid" label="独立云解压输出 CID" /></template>
        <template v-else><v-text-field v-model="newSource.path" label="授权的本地 / 容器路径" /><v-text-field v-if="newSource.kind !== 'inbox'" v-model="newSource.output_path" label="独立解压输出路径" /></template>
        <v-textarea v-if="['local', 'inbox'].includes(newSource.kind)" v-model="sourceExcludes" label="排除路径" rows="3" />
        <v-text-field v-if="newSource.kind !== '115'" v-model.number="newSource.interval" type="number" label="扫描间隔 / 秒" />
        <v-btn type="submit" color="primary">保存来源</v-btn>
      </v-form></v-card-text>
    </v-card></v-dialog>
    <v-dialog :model-value="!!confirmation" max-width="480" @update:model-value="!$event && (confirmation = null)"><v-card><v-card-title class="confirmation-title">{{ confirmation?.title }}</v-card-title><v-card-actions><v-spacer /><v-btn variant="text" @click="confirmation = null">返回</v-btn><v-btn color="error" @click="confirm">确认操作</v-btn></v-card-actions></v-card></v-dialog>
    <v-snackbar v-model="saveNoticeOpen" :color="saveSucceeded ? 'success' : 'error'" :timeout="6000">{{ saveFeedback }}</v-snackbar>
    <section v-if="view === 'passwords'" class="settings-section">
      <header class="section-heading"><h2>密码库</h2><span class="muted">{{ settings.password_count || 0 }} 条记录</span></header>
      <v-form class="password-add" @submit.prevent="action('/api/passwords', { action: 'add', password: newPassword }, '压缩密码已保存').then(async ok => { if (ok) { newPassword = ''; await refreshPasswords() } })">
        <v-text-field v-model="newPassword" label="新增压缩密码" type="text" autocomplete="off" />
        <v-btn type="submit" color="primary" :disabled="loading || !newPassword">保存密码</v-btn>
      </v-form><div class="table-wrap"><table><thead><tr><th>记录</th><th>密码</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="(password, index) in savedPasswords" :key="index"><td><strong>密码 {{ String(index + 1).padStart(2, '0') }}</strong></td><td class="mono path-cell">{{ password }}</td><td><span class="state-tag success">已保存</span></td><td><div class="row-actions"><button title="移除密码" :aria-label="'移除密码' + (index + 1)" @click="confirmation = { title: '移除此密码记录？', endpoint: '/api/passwords', body: { action: 'remove', index } }"><Trash2 :size="16" /></button></div></td></tr></tbody></table><div v-if="!savedPasswords.length" class="empty"><span>暂无密码记录</span></div></div>
    </section>
  </section>
</template>
