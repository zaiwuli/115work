export async function api<T = any>(path: string, body?: unknown, method?: string, signal?: AbortSignal): Promise<T> {
  const headers: Record<string, string> = {}
  const token = sessionStorage.getItem('115work-token')
  const csrf = sessionStorage.getItem('115work-csrf')
  if (token) headers.Authorization = `Bearer ${token}`
  if (csrf) headers['X-CSRF-Token'] = csrf
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  let response: Response
  try {
    response = await fetch(path, {
    method: method || (body === undefined ? 'GET' : 'POST'),
    credentials: 'same-origin',
    signal,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error
    throw new Error(body === undefined && (!method || method === 'GET')
      ? '详情或数据加载失败，无法连接后台，请稍后重试。'
      : '无法连接后台，操作结果尚未确认。请恢复连接后刷新核对，不要连续重复操作。')
  }
  const data = await response.json()
  if (!response.ok) throw new Error(errorLabel(data.error?.code || 'REQUEST_FAILED'))
  return data
}

export async function uploadDocument(file: File, autoExtract = true) {
  const headers: Record<string, string> = {}
  const token = sessionStorage.getItem('115work-token')
  const csrf = sessionStorage.getItem('115work-csrf')
  if (token) headers.Authorization = `Bearer ${token}`
  if (csrf) headers['X-CSRF-Token'] = csrf
  const form = new FormData()
  form.append('file', file)
  const torrent = file.name.toLowerCase().endsWith('.torrent')
  if (torrent) form.append('auto_extract', String(autoExtract))
  const response = await fetch(torrent ? '/api/v1/offline/import' : '/api/v1/documents/upload', {
    method: 'POST', credentials: 'same-origin', headers, body: form,
  })
  const data = await response.json()
  if (!response.ok) throw new Error(errorLabel(data.error?.code || 'UPLOAD_FAILED'))
  return data
}

export const stateNames: Record<string, string> = {
  UNKNOWN: '尚未检查', MATCHED: '连接正常', UNAVAILABLE: '连接不可用',
  AUTH_OR_CONNECTION_REQUIRED: '需要登录或检查连接',
  local: '本地目录', inbox: '链接收件箱', '115': '115 云盘',
  auto: '自动', cloud: '云端', ignore: '忽略', retain: '保留原包',
  archive: '归档', interval: '固定间隔', cron: '定时计划', event: '事件触发',
  READY: '等待处理', RUNNING: '处理中', WAITING_EXTERNAL: '等待远端',
  WAITING_RETRY: '等待重试', WAITING_APPROVAL: '等待批准',
  WAITING_RESOURCE: '等待资源', RECONCILING: '等待核对',
  SUCCEEDED: '完成', FAILED: '失败', CANCELED: '已取消', SKIPPED: '已跳过',
  queued: '等待提交', submitting: '提交中', submitted: '已提交',
  downloading: '下载中', success: '离线成功', failed: '离线失败',
  submit_failed: '提交失败', submission_unknown: '提交结果待核对', unknown: '状态待核对',
  pending: '待发送', sent: '已发送', suppressed: '已抑制',
  canceled: '已取消',
  IMPORTED: '已导入', ARCHIVED: '已归档', PARSED: '已解析',
}
export function errorLabel(value: string) {
  const names: Record<string, string> = {
    LOCAL_ACCESS_ONLY: '免登录后台仅允许本机访问，请使用本机地址打开。',
    CROSS_SITE_REQUEST_REJECTED: '已拒绝跨站请求，请从后台页面重新操作。',
    LOGIN_REQUIRED: '需要登录后台。',
    VALIDATION_ERROR: '填写内容不符合要求，请检查后重试。',
    INTERNAL_ERROR: '后台处理异常，请查看事件日志。',
    FALLBACK_CAPABILITY_REQUIRED: '当前原包暂不能下载，请检查 115 连接和文件状态。',
    CLOUD_PROCESSING_TIMEOUT: '云处理超过最长时间，已释放云队列，可选择下载到本地解压。',
    CLOUD_ORIGINAL_OPERATION_UNCONFIRMED: '本地解压已成功，但原云操作尚未确认结束，已保留云端原包；确认后可重试后处理。',
    DOWNLOAD_HOST_NOT_AUTHORIZED: '下载域名未在允许列表中，请查看任务详情的下载地址诊断。',
    DOWNLOAD_PRIVATE_ADDRESS_REJECTED: '下载域名解析到非公网地址，请检查DNS或代理配置。',
    DOWNLOAD_REQUEST_REJECTED: '下载服务器拒绝请求，请检查下载地址或登录状态。',
    INVALID_DOWNLOAD_HOSTS: '请填写有效域名，不含协议、端口、路径或通配符。',
    INVALID_LOCAL_LIMIT: '本地限制值无效，请填写非负整数，文件数量至少为1。',
    ARCHIVE_SIZE_LIMIT: '本地解压输出总量超过设置上限。',
    ARCHIVE_FILE_LIMIT: '本地解压文件数量超过设置上限。',
    INSUFFICIENT_OUTPUT_SPACE: '输出盘剩余空间低于预留空间，已停止本地解压。',
    FAILED_ARCHIVE_RECONCILIATION_REQUIRED: '请先核对失败原包的归档结果。',
    SOURCE_UNAVAILABLE: '来源目录不可用，请检查路径或挂载状态。',
    SOURCE_SCAN_BUSY: '该目录正在扫描或等待核对，请稍后删除来源。',
    SOURCE_NOT_FOUND: '此来源已不存在，请刷新页面。',
    PREVIEW_READ_ONLY: '模拟预览为只读，不执行真实操作。',
    INVALID_CLEANUP_SUFFIXES: '请填写有效后缀，例如 .url、.lnk；不支持通配符。',
    REQUEST_FAILED: '请求未成功，请检查后台是否正常运行。',
    CLOUD_ROOT_REQUIRED: '请先在系统设置中初始化115云根目录。',
    CLOUD_CONFIGURATION_BUSY: '当前有任务执行，请稍后初始化云目录。',
    LIFE_MONITOR_DISABLED: '请先启用生活事件监测。',
    INVALID_MONITOR_SETTING: '监测开关值无效。',
    TERMINAL_TASK_REQUIRED: '任务尚未结束，暂不能删除。',
    TASK_OPERATIONS_PENDING: '任务仍有执行中或待核对的操作，请稍后再试。',
    OFFLINE_OPERATION_PENDING: '离线任务仍在执行或结果不明确，暂不能删除。',
    CONNECTION_SWITCH_BUSY: '当前任务尚未结束，Cookie已保存，请稍后重新启用连接。',
    CONNECTION_DELETE_BUSY: '当前有任务正在执行，请稍后再删除账户。',
    INVALID_TORRENT: '种子文件无效或内容不完整，请选择有效的.torrent文件。',
    TORRENT_V2_UNSUPPORTED: '暂不支持纯BT v2种子，请使用BT v1或混合种子。',
    TORRENT_SIZE_LIMIT: '种子文件不能为空且不能超过2MB。',
    TORRENT_IMPORT_SEPARATELY: '种子文件与粘贴链接请分别导入。',
    CONNECTION_NOT_FOUND: '账户已不存在，请刷新页面。',
    FEATURE_REMOVED: '此功能已移除。',
    SOURCE_OUTPUT_LOOP: '监测目录不能是工具根目录、解压目录、归档目录或失败目录。',
    UPLOAD_FAILED: '文档上传失败，请重试。',
  }
  return names[value] || (/[a-z]/i.test(value)
    ? '操作未完成，请检查配置并查看后台运行日志。' : value)
}
export const stageNames: Record<string, string> = {
  offline_submit: '提交离线', offline_download: '离线下载', discover: '等待发现文件',
  offline_manual: '等待手动查询',
  cloud_prepare: '等待云解压', cloud_inspect: '云解压分析', cloud_output: '准备云端输出',
  cloud_progress: '云解压进行中', cloud_verify: '确认云端完成', cloud_export_unknown: '提交结果核对',
  cloud_password: '等待密码', local_fallback: '本地兜底待批准', cache: '缓存文件',
  local_extract: '本地解压', local_capability: '等待解压能力', registered: '文件登记', complete: '完成',
  document_copy: '文档处理', postprocess: '原包后处理', join: '多个关联产物',
  legacy_reconcile: '旧记录待核对',
  identity_conflict: '内容身份冲突', failure_archive: '失败原包归档',
  cloud_monitor: '生活事件监测',
  output_cleanup: '清理解压产物',
  discover_review: '文件发现待核对', join_review: '多个产物范围待核对',
}
export function stateLabel(value: string) { return stateNames[value] || (/^[a-z_]+$/i.test(value || '') ? '状态待核对' : value) }
export function statusTone(state: string) {
  if (['SUCCEEDED', 'success', 'sent', 'ARCHIVED'].includes(state)) return 'success'
  if (['FAILED', 'failed', 'submit_failed'].includes(state)) return 'failure'
  if (state.includes('APPROVAL') || state.includes('RETRY') || state.includes('unknown')
    || ['RECONCILING', 'WAITING_RESOURCE'].includes(state)) return 'waiting'
  if (['RUNNING', 'WAITING_EXTERNAL', 'downloading', 'submitted', 'submitting'].includes(state)) return 'running'
  return 'neutral'
}
export function stageLabel(value: string) { return stageNames[value] || ({
  local_scan: '扫描本地目录', cloud_scan: '扫描云目录', offline_poll: '查询离线状态',
  notifications: '发送通知', cache_gc: '清理缓存', document: '解析文档', system: '系统操作',
}[value] || '处理记录') }
export function textLabel(value: string) {
  if (!value || !/[a-z]/i.test(value)) return value
  if (/[\u4e00-\u9fff]/.test(value)) return value
  const events: Record<string, string> = {
    remote_reported_complete: '115报告解压完成（未全量核对）',
    remote_manifest: '云端文件清单核对通过',
    CLOUD_ROOT_INITIALIZED: '云目录初始化', LIFE_CHECKED: '生活事件检查',
    SCAN_COMPLETED: '云目录扫描完成', LOCAL_SCAN_COMPLETED: '本地扫描完成',
    DISCOVERY_SCHEDULED: '离线目录扫描排队', DUPLICATE_SKIPPED: '重复资源已跳过',
    DISCOVERED: '发现文件', CLOUD_TAKEOVER: '云解压接管', SUBMITTED: '离线已提交',
    REGISTERED: '登记任务', IMPORT: '导入离线任务', JOB_FAILED: '执行失败',
  }
  if (events[value]) return events[value]
  if (stateNames[value]) return stateNames[value]
  if (stageNames[value]) return stageNames[value]
  if (/^[A-Z][A-Z0-9_]+$/.test(value)) return errorLabel(value)
  const names: Record<string, string> = {
    'Local archives': '本地压缩包', 'Link inbox': '链接收件箱',
    'local-admin': '本机管理员', 'admin-token': '管理员',
    'cloud': '云解压', 'local': '本地解压', 'offline': '离线下载',
    'pending': '等待处理', 'restart_required': '需要重启',
    'verified_manifest': '已校验产物清单', 'manifest': '产物清单',
    'Original archive moved; local fallback remains independent': '原包已归档，仍等待本地解压确认',
  }
  return names[value] || '已记录处理结果，请结合当前阶段和状态查看。'
}
export function date(value: number | null) {
  return value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '-'
}
export function bytes(value: number) {
  if (!value) return '0 B'
  const index = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)))
  return `${(value / 1024 ** index).toFixed(index ? 1 : 0)} ${['B', 'KB', 'MB', 'GB', 'TB'][index]}`
}
