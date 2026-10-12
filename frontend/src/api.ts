export async function api<T = any>(path: string, body?: unknown, method?: string): Promise<T> {
  const headers: Record<string, string> = {}
  const token = sessionStorage.getItem('115work-token')
  const csrf = sessionStorage.getItem('115work-csrf')
  if (token) headers.Authorization = `Bearer ${token}`
  if (csrf) headers['X-CSRF-Token'] = csrf
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const response = await fetch(path, {
    method: method || (body === undefined ? 'GET' : 'POST'),
    credentials: 'same-origin',
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok) throw new Error(errorLabel(data.error?.code || 'REQUEST_FAILED'))
  return data
}

export async function uploadDocument(file: File) {
  const headers: Record<string, string> = {}
  const token = sessionStorage.getItem('115work-token')
  const csrf = sessionStorage.getItem('115work-csrf')
  if (token) headers.Authorization = `Bearer ${token}`
  if (csrf) headers['X-CSRF-Token'] = csrf
  const form = new FormData()
  form.append('file', file)
  const response = await fetch('/api/v1/documents/upload', {
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
    FAILED_ARCHIVE_RECONCILIATION_REQUIRED: '请先核对失败原包的归档结果。',
    SOURCE_UNAVAILABLE: '来源目录不可用，请检查路径或挂载状态。',
    REQUEST_FAILED: '请求未成功，请检查后台是否正常运行。',
    UPLOAD_FAILED: '文档上传失败，请重试。',
  }
  return names[value] || (/[a-z]/i.test(value)
    ? '操作未完成，请检查配置并查看后台运行日志。' : value)
}
export const stageNames: Record<string, string> = {
  offline_submit: '提交离线', offline_download: '离线下载', discover: '等待发现文件',
  cloud_prepare: '等待云解压', cloud_inspect: '云解压分析', cloud_output: '准备云端输出',
  cloud_progress: '云端导出中', cloud_verify: '云端结果校验', cloud_export_unknown: '提交结果核对',
  cloud_password: '等待密码', local_fallback: '本地兜底待批准', cache: '缓存文件',
  local_extract: '本地解压', local_capability: '等待解压能力', registered: '文件登记', complete: '完成',
  document_copy: '文档处理', postprocess: '原包后处理', join: '多个关联产物',
  legacy_reconcile: '旧记录待核对',
  identity_conflict: '内容身份冲突', failure_archive: '失败原包归档',
  discover_review: '文件发现待核对', join_review: '多个产物范围待核对',
}
export function stateLabel(value: string) { return stateNames[value] || (/^[a-z_]+$/i.test(value || '') ? '状态待核对' : value) }
export function stageLabel(value: string) { return stageNames[value] || ({
  local_scan: '扫描本地目录', cloud_scan: '扫描云目录', offline_poll: '查询离线状态',
  notifications: '发送通知', cache_gc: '清理缓存', document: '解析文档', system: '系统操作',
}[value] || '处理记录') }
export function textLabel(value: string) {
  if (!value || !/[a-z]/i.test(value)) return value
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
