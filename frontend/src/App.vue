<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useDisplay, useTheme } from 'vuetify'
import { useRoute, useRouter } from 'vue-router'
import {
  Activity, LayoutDashboard, ListTodo, Download, FileInput, FolderOpen,
  CalendarClock, History, Bell, Settings, Shield, Menu, Pause, Play, RefreshCw,
  LogOut, ChevronRight, HardDrive, LockKeyhole, Network,
  Plug, GitBranch, KeyRound, FileClock,
  Sun, Moon,
} from 'lucide-vue-next'
import { api } from './api'
import { useSystem } from './store'

const system = useSystem()
const theme = useTheme()
const display = useDisplay()
const themeMode = ref(localStorage.getItem('115work-theme') || 'light')
theme.global.name.value = themeMode.value
function toggleTheme() {
  themeMode.value = themeMode.value === 'light' ? 'dark' : 'light'
  theme.global.name.value = themeMode.value
  localStorage.setItem('115work-theme', themeMode.value)
}
const route = useRoute()
const router = useRouter()
const logged = ref(false)
const localNoAuth = ref(false)
const configured = ref(true)
const token = ref('')
const password = ref('')
const loginError = ref('')
const loading = ref(false)
const drawer = ref(window.innerWidth >= 960)
watch(display.mdAndUp, value => { drawer.value = value })
let timer: ReturnType<typeof setInterval> | undefined
let events: EventSource | undefined
const navigation = [
  { key: 'overview', title: '总览', icon: LayoutDashboard },
  { key: 'tasks', title: '任务中心', icon: ListTodo },
  { key: 'offline', title: '离线下载', icon: Download },
  { key: 'documents', title: '文档导入', icon: FileInput },
  { key: 'sources', title: '来源管理', icon: FolderOpen },
  { key: 'rules', title: '处理规则', icon: GitBranch },
  { key: 'schedules', title: '自动计划', icon: FileClock },
  { key: 'jobs', title: '自动任务', icon: CalendarClock },
  { key: 'history', title: '处理历史', icon: History },
  { key: 'notifications', title: '通知记录', icon: Bell },
  { key: 'logs', title: '事件日志', icon: Activity },
  { key: 'connections', title: '连接管理', icon: Plug },
  { key: 'audit', title: '操作审计', icon: Shield },
  { key: 'settings', title: '系统设置', icon: Settings },
  { key: 'maintenance', title: '数据维护', icon: Shield },
]
const title = computed(() => navigation.find(x => x.key === (route.params.view || 'overview'))?.title || '总览')

async function connect() {
  logged.value = true
  await system.refresh()
  timer = setInterval(() => void system.refresh(), 10000)
  if (!sessionStorage.getItem('115work-token')) {
    events = new EventSource('/api/v1/events')
    events.onmessage = () => void system.refresh()
  }
}
async function login() {
  loading.value = true
  loginError.value = ''
  try {
    if (!configured.value) {
      sessionStorage.setItem('115work-token', token.value)
      await api('/api/auth/setup', { password: password.value })
      sessionStorage.removeItem('115work-token')
      configured.value = true
    } else if (token.value && !password.value) {
      sessionStorage.setItem('115work-token', token.value)
      await api('/api/status')
      await connect()
      return
    }
    const response = await api('/api/auth/login', { password: password.value })
    sessionStorage.setItem('115work-csrf', response.csrf_token)
    password.value = ''
    token.value = ''
    await connect()
  } catch (error) {
    sessionStorage.removeItem('115work-token')
    loginError.value = (error as Error).message
  } finally {
    loading.value = false
  }
}
async function logout() {
  if (!sessionStorage.getItem('115work-token')) await api('/api/auth/logout', {})
  sessionStorage.removeItem('115work-token')
  sessionStorage.removeItem('115work-csrf')
  events?.close()
  clearInterval(timer)
  logged.value = false
}
async function control() {
  try { await system.command('/api/tasks/system', { action: system.status?.paused ? 'resume' : 'pause' }) }
  catch (error) { system.error = (error as Error).message }
}
onMounted(async () => {
  const status = await api('/api/auth/status').catch(() => ({ configured: true, authenticated: false }))
  configured.value = status.configured
  localNoAuth.value = !!status.local_no_auth
  if (status.authenticated) {
    if (status.csrf_token) sessionStorage.setItem('115work-csrf', status.csrf_token)
    await connect()
  }
})
onBeforeUnmount(() => { clearInterval(timer); events?.close() })
</script>

<template>
  <v-app>
    <main v-if="!logged" class="login-page">
      <section class="login-form">
        <div class="login-brand"><HardDrive :size="32" /><h1>115work</h1></div>
        <h2>{{ configured ? '管理登录' : '初始化管理员' }}</h2>
        <v-form @submit.prevent="login">
          <v-text-field v-if="!configured" v-model="token" type="password" label="初始化令牌" autocomplete="off" class="mb-5" />
          <v-text-field v-model="password" type="password" label="管理员密码" :autocomplete="configured ? 'current-password' : 'new-password'" class="mb-5" />
          <v-expansion-panels v-if="configured" variant="accordion" class="mb-5">
            <v-expansion-panel title="使用管理令牌">
              <v-expansion-panel-text><v-text-field v-model="token" type="password" label="管理令牌" autocomplete="off" /></v-expansion-panel-text>
            </v-expansion-panel>
          </v-expansion-panels>
          <p v-if="loginError" role="alert" class="error-text">{{ loginError }}</p>
          <v-btn type="submit" color="primary" block :loading="loading" height="44"><LockKeyhole :size="17" class="mr-2" />{{ configured ? '登录' : '创建管理员' }}</v-btn>
        </v-form>
      </section>
    </main>
    <template v-else>
      <v-navigation-drawer v-model="drawer" width="224" :temporary="$vuetify.display.smAndDown" class="sidebar">
        <div class="brand"><HardDrive :size="26" /><strong>115work</strong><span>0.1</span></div>
        <div class="nav-caption">工作空间</div>
        <nav aria-label="主导航">
          <button v-for="item in navigation.filter(x => system.status?.role !== 'readonly' || !['security','audit','maintenance','connections','settings','offline'].includes(x.key))" :key="item.key" class="nav-item"
            :class="{ selected: (route.params.view || 'overview') === item.key }"
            @click="router.push('/' + item.key); $vuetify.display.smAndDown && (drawer = false)">
            <component :is="item.icon" :size="18" /><span>{{ item.title }}</span>
          </button>
        </nav>
        <template #append>
          <div class="sidebar-bottom"><Network :size="16" /><span>{{ system.status?.adapter === 'disabled' ? '115 未连接' : system.status?.adapter === 'fake' ? '测试适配器' : '115 已配置' }}</span></div>
        </template>
      </v-navigation-drawer>
      <v-app-bar flat height="64" border="b">
        <v-btn icon aria-label="展开导航" title="展开导航" class="ml-2" @click="drawer = !drawer"><Menu :size="20" /></v-btn>
        <div class="breadcrumb"><span>工作空间</span><ChevronRight :size="14" /><strong>{{ title }}</strong></div>
        <v-spacer />
        <span class="connection"><i :class="{ disconnected: !system.connected }" />{{ system.connected ? '已连接' : '连接中断' }}</span>
        <v-btn v-if="system.status?.role !== 'readonly'" icon :title="system.status?.paused ? '恢复任务' : '暂停任务'" :aria-label="system.status?.paused ? '恢复任务' : '暂停任务'" :disabled="system.busy" @click="control"><component :is="system.status?.paused ? Play : Pause" :size="18" /></v-btn>
        <v-btn icon title="切换主题" aria-label="切换主题" @click="toggleTheme"><component :is="themeMode === 'light' ? Moon : Sun" :size="18" /></v-btn>
        <v-btn icon title="刷新状态" aria-label="刷新状态" @click="system.refresh()"><RefreshCw :size="18" /></v-btn>
        <v-btn v-if="!localNoAuth" icon title="退出登录" aria-label="退出登录" class="mr-2" @click="logout"><LogOut :size="18" /></v-btn>
      </v-app-bar>
      <v-main>
        <div v-if="system.status?.paused" class="pause-banner"><Pause :size="16" />任务系统已暂停</div>
        <RouterView />
      </v-main>
      <v-snackbar :model-value="!!system.error" color="error" :timeout="6000" @update:model-value="!$event && (system.error = '')">{{ system.error }}</v-snackbar>
    </template>
  </v-app>
</template>
