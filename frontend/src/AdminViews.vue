<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Plus, RefreshCw, X, Save, Play, Shield, Link, KeyRound, Search, Trash2, Archive, Check, FileClock } from 'lucide-vue-next'
import { api, date, stateLabel, textLabel } from './api'
import { useSystem } from './store'
const system = useSystem()
const canWrite = computed(() => system.status?.role !== 'readonly')

const route = useRoute()
const view = computed(() => String(route.params.view))
const titles: Record<string, string> = { connections: '连接管理', rules: '处理规则', schedules: '自动计划', security: '安全管理', logs: '事件日志', audit: '操作审计' }
const items = ref<any[]>([])
const sources = ref<any[]>([])
const error = ref('')
const message = ref('')
const busy = ref(false)
const opened = ref(false)
const editing = ref<any>(null)
const name = ref('')
const kind = ref('115')
const url = ref('')
const root = ref('/')
const credential = ref('')
const sourceId = ref('')
const pattern = ref('*')
const originalAction = ref('retain')
const documentAction = ref('register')
const workflow = ref('auto')
const autoFallback = ref(false)
const disableRecursion = ref(false)
const recurseIso = ref(false)
const squashRoot = ref(true)
const extractIsos = ref(true)
const archiveCid = ref('')
const archivePath = ref('')
const failedCid = ref('')
const documentLinks = ref(false)
const trigger = ref('interval')
const interval = ref(300)
const expression = ref('0 1 * * *')
const timezone = ref('Asia/Shanghai')
const role = ref('readonly')
const tokenName = ref('')
const ttl = ref(30)
const createdToken = ref('')
const newPassword = ref('')
const previewName = ref('')
const preview = ref<any>(null)
const diagnosis = ref<any>(null)
const qr = ref<any>(null)
const directory = ref('0')
const browser = ref<any[]>([])
const browseConnection = ref('')

async function load() {
  busy.value = true
  error.value = ''
  try {
    const path = view.value === 'security' ? 'security/tokens' : view.value
    items.value = (await api('/api/v1/' + path)).items
    sources.value = (await api('/api/v1/sources')).items
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function command(path: string, body: unknown = {}, method?: string) {
  busy.value = true
  error.value = ''
  try {
    const result = await api(path, body, method)
    message.value = '操作成功'
    await load()
    return result
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
function open(item?: any) {
  editing.value = item || null
  name.value = item?.name || ''
  sourceId.value = item?.source_id || ''
  pattern.value = item?.config.pattern || '*'
  originalAction.value = item?.config.original_action || 'retain'
  documentAction.value = item?.config.document_action || 'register'
  workflow.value = item?.config.workflow || 'auto'
  autoFallback.value = item?.config.auto_fallback || false
  disableRecursion.value = item?.config.disable_recursion ?? false
  recurseIso.value = item?.config.recurse_iso ?? false
  squashRoot.value = item?.config.squash_root ?? true
  extractIsos.value = item?.config.extract_isos ?? true
  archiveCid.value = item?.config.archive_cid || ''
  archivePath.value = item?.config.archive_path || ''
  failedCid.value = item?.config.cloud_failed_cid || ''
  documentLinks.value = item?.config.document_links || false
  trigger.value = item?.trigger || 'interval'
  interval.value = item?.config.seconds || 300
  expression.value = item?.config.expression || '0 1 * * *'
  timezone.value = item?.timezone || 'Asia/Shanghai'
  opened.value = true
}
async function save() {
  let result
  if (view.value === 'connections') {
    result = await command('/api/v1/connections', { name: name.value, kind: kind.value,
      config: { root_cid: directory.value },
      credential: credential.value })
  } else if (view.value === 'rules') {
    const config = { pattern: pattern.value, original_action: originalAction.value,
      document_action: documentAction.value, workflow: workflow.value, auto_fallback: false,
      document_links: documentLinks.value,
      disable_recursion: disableRecursion.value, recurse_iso: recurseIso.value,
      squash_root: squashRoot.value,
      extract_isos: extractIsos.value,
      ...(archiveCid.value ? { archive_cid: archiveCid.value } : {}),
      ...(archivePath.value ? { archive_path: archivePath.value } : {}),
      ...(failedCid.value ? { cloud_failed_cid: failedCid.value } : {}) }
    const body = { name: name.value, source_id: sourceId.value, config,
      ...(editing.value ? { expected_revision: editing.value.revision } : {}) }
    result = await command('/api/v1/rules' + (editing.value ? '/' + editing.value.id : ''), body,
      editing.value ? 'PUT' : 'POST')
  } else {
    result = await command('/api/v1/schedules' + (editing.value ? '/' + editing.value.id : ''), { name: name.value, source_id: sourceId.value,
      trigger: trigger.value, config: trigger.value === 'cron' ? { expression: expression.value } : { seconds: interval.value },
      timezone: timezone.value, misfire: 'coalesce',
      ...(editing.value ? { expected_revision: editing.value.revision, enabled: editing.value.enabled } : {}),
    }, editing.value ? 'PUT' : 'POST')
  }
  if (result) { opened.value = false; credential.value = '' }
}
async function browse(item: any) {
  browseConnection.value = item.id
  const body = item.kind === '115' ? { cid: directory.value } : { path: item.config.root }
  const result = await command('/api/v1/connections/' + item.id + '/browse', body)
  if (result) browser.value = result.items
}
function reload() { window.location.reload() }
watch(view, () => { items.value = []; message.value = ''; void load() }, { immediate: true })
</script>

<template>
  <section class="workspace">
    <header class="page-heading"><div><div class="eyebrow">115work</div><h1>{{ titles[view] }}</h1></div>
      <div class="heading-actions"><v-btn v-if="canWrite && ['connections','rules','schedules'].includes(view)" color="primary" @click="open()"><Plus :size="17" class="mr-2" />添加</v-btn>
        <v-btn icon variant="text" aria-label="刷新页面" title="刷新页面" @click="load"><RefreshCw :size="18" /></v-btn></div>
    </header>
    <p v-if="error" role="alert" class="error-text alert">{{ error }}</p>
    <p v-if="message" role="status" class="success-text alert">{{ message }}</p>
    <v-progress-linear v-if="busy" indeterminate color="primary" height="2" />

    <template v-if="view === 'connections'">
      <div class="table-wrap"><table><thead><tr><th>连接</th><th>类型 / 协议</th><th>健康</th><th>凭据</th><th>操作</th></tr></thead><tbody>
        <tr v-for="item in items" :key="item.id"><td><strong>{{ item.name }}</strong><small>{{ item.config.url || ('CID ' + item.config.root_cid) }}</small></td>
          <td>{{ stateLabel(item.kind) }}</td><td>{{ stateLabel(item.health) }}</td><td>{{ item.credential_configured ? '已加密保存' : '未配置' }}<small>版本 {{ item.revision }}</small></td>
          <td><div class="row-actions"><button aria-label="诊断连接" title="诊断连接" @click="command('/api/v1/connections/' + item.id + '/diagnose').then(x => diagnosis = x)"><Shield :size="18" /></button>
            <button v-if="item.kind === '115'" aria-label="启用115连接" title="启用115连接" @click="command('/api/v1/connections/' + item.id + '/activate')"><Play :size="18" /></button>
            <button aria-label="浏览目录" title="浏览目录" @click="browse(item)"><Search :size="18" /></button>
            <button aria-label="更新凭据" title="更新凭据" @click="editing = item; credential = ''; opened = true"><KeyRound :size="18" /></button></div></td></tr>
      </tbody></table><div v-if="!items.length" class="empty"><Link :size="32" /><span>暂无连接</span></div></div>
      <p v-if="diagnosis" class="mono mt-5">{{ stateLabel(diagnosis.status) }}</p>
      <section v-if="browser.length" class="settings-section"><h2>目录列表</h2><div v-for="file in browser" :key="file.id || file.fid" class="event-line"><span>{{ file.name }}</span><small>{{ file.path || file.cid }} · {{ file.size }}</small></div></section>
      <section class="settings-section"><h2>115 扫码登录</h2><v-btn variant="outlined" :disabled="busy" @click="command('/api/v1/auth/115/qrcode').then(x => qr = x)"><KeyRound :size="17" class="mr-2" />生成二维码</v-btn>
        <div v-if="qr" class="qr-area"><img :src="qr.image" alt="115 登录二维码" width="220" height="220" /><v-btn variant="outlined" @click="command('/api/v1/auth/115/qrcode/' + qr.id).then(x => message = x?.status || '')"><Check :size="17" class="mr-2" />查询扫码状态</v-btn></div>
      </section>
    </template>

    <template v-else-if="view === 'rules'">
      <div class="table-wrap"><table><thead><tr><th>规则</th><th>来源</th><th>匹配</th><th>流程</th><th>原包策略</th><th>操作</th></tr></thead><tbody>
        <tr v-for="item in items" :key="item.id"><td><strong>{{ item.name }}</strong><small>版本 {{ item.revision }}</small></td><td>{{ sources.find(x => x.id === item.source_id)?.name || '所有来源' }}</td><td>{{ item.config.pattern || '*' }}</td><td>{{ stateLabel(item.config.workflow || 'auto') }}</td><td>{{ stateLabel(item.config.original_action || 'retain') }}</td>
          <td><button v-if="canWrite" class="file-link" aria-label="编辑规则" title="编辑规则" @click="open(item)"><Save :size="17" /></button></td></tr>
      </tbody></table><div v-if="!items.length" class="empty"><Archive :size="32" /><span>暂无规则</span></div></div>
      <section v-if="canWrite" class="settings-section"><h2>规则预览</h2><div class="form-grid"><v-text-field v-model="previewName" label="文件名" /><v-btn variant="outlined" :disabled="!previewName" @click="command('/api/v1/rules/preview', { name: previewName, size: 0, source_id: sourceId }).then(x => preview = x)"><Search :size="17" class="mr-2" />预览</v-btn></div><p v-if="preview" class="mono mt-4">{{ stateLabel(preview.policy.workflow || 'auto') }}</p></section>
    </template>

    <template v-else-if="view === 'schedules'">
      <div class="table-wrap"><table><thead><tr><th>计划</th><th>触发</th><th>下次运行</th><th>时区</th><th>启用</th><th>操作</th></tr></thead><tbody>
        <tr v-for="item in items" :key="item.id"><td><strong>{{ item.name }}</strong><small>{{ sources.find(x => x.id === item.source_id)?.name || '维护' }}</small></td><td>{{ stateLabel(item.trigger) }}<small>{{ item.config.expression || (item.config.seconds + ' 秒') }}</small></td><td>{{ date(item.next_run) }}</td><td>{{ item.timezone }}</td><td><v-switch :model-value="item.enabled" :disabled="!canWrite" color="primary" hide-details :aria-label="'启用' + item.name" @update:model-value="command('/api/v1/schedules/' + item.id, { enabled: !!$event, expected_revision: item.revision }, 'PATCH')" /></td>
          <td><div v-if="canWrite" class="row-actions"><button class="file-link" aria-label="编辑计划" title="编辑计划" @click="open(item)"><Save :size="17" /></button><button class="file-link" aria-label="立即执行" title="立即执行" @click="command('/api/v1/schedules/' + item.id + '/run')"><Play :size="17" /></button></div></td></tr>
      </tbody></table><div v-if="!items.length" class="empty"><FileClock :size="32" /><span>暂无自动计划</span></div></div>
    </template>

    <template v-else-if="view === 'security'">
      <section class="settings-section"><h2>访问令牌</h2><div class="form-grid"><v-text-field v-model="tokenName" label="令牌名称" /><v-select v-model="role" :items="[{ title: '只读观察者', value: 'readonly' }, { title: '链接导入', value: 'import' }]" label="权限" /><v-text-field v-model.number="ttl" type="number" min="1" max="365" label="有效天数" /><v-btn color="primary" @click="command('/api/v1/security/tokens', { role, name: tokenName, ttl_days: ttl }).then(x => createdToken = x?.token || '')"><Plus :size="17" class="mr-2" />生成令牌</v-btn></div>
        <v-text-field v-if="createdToken" :model-value="createdToken" label="新令牌" readonly class="mt-5" />
        <div v-for="item in items" :key="item.id" class="event-line"><div class="flex-content"><strong>{{ item.name }}</strong><small>{{ item.role }} · {{ date(item.expires) }}</small></div><button class="file-link" aria-label="撤销令牌" title="撤销令牌" @click="command('/api/v1/security/tokens/' + item.id, undefined, 'DELETE')"><Trash2 :size="17" /></button></div>
      </section>
      <section class="settings-section"><h2>管理员密码</h2><div class="form-grid"><v-text-field v-model="newPassword" type="password" label="新密码" autocomplete="new-password" /><v-btn variant="outlined" :disabled="newPassword.length < 12" @click="command('/api/v1/security/password', { password: newPassword }).then(x => x && reload())"><Save :size="17" class="mr-2" />更新密码</v-btn></div></section>
    </template>

    <template v-else>
      <div class="table-wrap"><table><thead><tr><th>时间</th><th>事件 / 操作</th><th>阶段 / 对象</th><th>详情</th></tr></thead><tbody>
        <tr v-for="item in items" :key="item.id"><td>{{ date(item.created_at) }}</td><td>{{ textLabel(item.kind || item.action) }}<small>{{ textLabel(item.actor || '') }}</small></td><td>{{ textLabel(item.stage || item.target) }}</td><td class="path-cell">{{ textLabel(item.message || JSON.stringify(item.details)) }}</td></tr>
      </tbody></table><div v-if="!items.length" class="empty">暂无记录</div></div>
    </template>

    <v-dialog v-model="opened" max-width="600"><v-card><header class="dialog-heading"><h2>{{ editing ? '编辑' : '添加' }}{{ titles[view] }}</h2><v-btn icon variant="text" aria-label="关闭" @click="opened = false"><X :size="20" /></v-btn></header>
      <v-card-text><v-form class="dialog-form" @submit.prevent="editing && view === 'connections' ? command('/api/v1/connections/' + editing.id + '/credentials', { credential, expected_revision: editing.revision }).then(x => x && (opened = false)) : save()">
        <template v-if="view === 'connections'"><v-text-field v-if="!editing" v-model="name" label="连接名称" /><v-select v-if="!editing" v-model="kind" :items="[{ title: '115 Cookie', value: '115' }]" label="类型" /><v-text-field v-if="!editing && kind === '115'" v-model="directory" label="根 CID" /><v-text-field v-model="credential" type="password" label="115 Cookie" autocomplete="off" /></template>
        <template v-else><v-text-field v-model="name" label="名称" /><v-select v-model="sourceId" :items="[{ title: '所有来源', value: '' }, ...sources.map(x => ({ title: x.name, value: x.id }))]" label="来源" aria-label="来源" />
          <template v-if="view === 'rules'">
            <v-text-field v-model="pattern" label="文件名模式" />
            <v-select v-model="workflow" :items="[{ title: '自动', value: 'auto' }, { title: '本地', value: 'local' }, { title: '云端', value: 'cloud' }, { title: '忽略', value: 'ignore' }]" label="工作流" />
            <v-select v-model="originalAction" :items="[{ title: '保留', value: 'retain' }, { title: '归档', value: 'archive' }]" label="原包策略" />
            <v-select v-model="documentAction" :items="[{ title: '只读登记', value: 'register' }, { title: '复制', value: 'copy' }, { title: '归档', value: 'archive' }]" label="普通文档" />
            <v-text-field v-model="archiveCid" label="云原包归档 CID" />
            <v-text-field v-model="failedCid" label="云解压失败归档 CID" />
            <v-switch v-model="documentLinks" label="提取云文档中的链接" color="primary" />
            <v-switch v-model="disableRecursion" label="禁止递归解压" color="primary" />
            <v-switch v-model="recurseIso" label="解压 ISO 内压缩包" color="primary" />
            <v-switch v-model="squashRoot" label="展开同名根目录" color="primary" />
          </template>
          <template v-else><v-select v-model="trigger" :items="[{ title: '固定间隔', value: 'interval' }, { title: 'Cron', value: 'cron' }]" label="触发" /><v-text-field v-if="trigger === 'interval'" v-model.number="interval" type="number" min="15" label="间隔 / 秒" /><v-text-field v-else v-model="expression" label="Cron 表达式" /><v-text-field v-model="timezone" label="时区" /></template>
        </template>
        <v-switch v-if="view === 'rules'" v-model="extractIsos" label="处理 ISO 镜像" color="primary" />
        <v-btn type="submit" color="primary" :disabled="busy"><Save :size="17" class="mr-2" />保存</v-btn>
      </v-form></v-card-text></v-card></v-dialog>
  </section>
</template>
