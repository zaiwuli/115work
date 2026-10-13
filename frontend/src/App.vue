<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useDisplay } from 'vuetify'
import { useRoute, useRouter } from 'vue-router'
import {
  Activity, LayoutDashboard, ListTodo, Download, FileInput, FolderOpen,
  CalendarClock, History, Bell, Settings, Shield, Menu, Pause, Play, RefreshCw,
  ChevronRight, HardDrive, Network,
  Plug, GitBranch, KeyRound, FileClock,
} from 'lucide-vue-next'
import { useSystem } from './store'

const system = useSystem()
const display = useDisplay()
const route = useRoute()
const router = useRouter()
const drawer = ref(window.innerWidth >= 960)
watch(display.mdAndUp, value => { drawer.value = value })
let timer: ReturnType<typeof setInterval> | undefined
let events: EventSource | undefined
const navigation = [
  { key: 'tasks', title: '任务中心', icon: ListTodo },
  { key: 'offline', title: '离线下载', icon: Download },
  { key: 'sources', title: '来源管理', icon: FolderOpen },
  { key: 'rules', title: '处理规则', icon: GitBranch },
  { key: 'passwords', title: '密码维护', icon: KeyRound },
  { key: 'jobs', title: '自动任务', icon: CalendarClock },
  { key: 'history', title: '处理历史', icon: History },
  { key: 'notifications', title: '通知记录', icon: Bell },
  { key: 'logs', title: '工作日志', icon: Activity },
  { key: 'settings', title: '系统设置', icon: Settings },
  { key: 'maintenance', title: '数据维护', icon: Shield },
]
const currentNavigation = computed(() => route.meta.view === 'connections' ? 'settings' : route.params.view || 'tasks')
const title = computed(() => navigation.find(x => x.key === currentNavigation.value)?.title || '任务中心')

async function connect() {
  await system.refresh()
  startAutoRefresh()
}
function startAutoRefresh() {
  clearInterval(timer)
  events?.close()
  if (!system.autoRefresh) return
  timer = setInterval(() => void system.refresh(), 10000)
  if (!sessionStorage.getItem('115work-token')) {
    events = new EventSource('/api/v1/events')
    events.onmessage = () => void system.refresh()
  }
}
watch(() => system.autoRefresh, startAutoRefresh)
async function control() {
  try { await system.command('/api/tasks/system', { action: system.status?.paused ? 'resume' : 'pause' }) }
  catch (error) { system.error = (error as Error).message }
}
onMounted(() => void connect())
onBeforeUnmount(() => { clearInterval(timer); events?.close() })
</script>

<template>
  <v-app>
      <v-navigation-drawer v-model="drawer" width="224" :temporary="$vuetify.display.smAndDown" class="sidebar">
        <div class="brand"><HardDrive :size="26" /><strong>115work</strong><span>0.1</span></div>
        <div class="nav-caption">工作空间</div>
        <nav aria-label="主导航">
          <button v-for="item in navigation.filter(x => system.status?.role !== 'readonly' || !['security','audit','maintenance','connections','settings','offline'].includes(x.key))" :key="item.key" class="nav-item"
            :class="{ selected: currentNavigation === item.key }"
            @click="router.push('/' + item.key); $vuetify.display.smAndDown && (drawer = false)">
            <component :is="item.icon" :size="18" /><span>{{ item.title }}</span>
          </button>
        </nav>
        <template #append>
          <button class="sidebar-bottom" title="查看运行状态" aria-label="查看115运行状态"
            @click="router.push('/settings?section=status'); $vuetify.display.smAndDown && (drawer = false)">
            <Network :size="16" /><span>{{ system.status?.adapter === 'disabled' ? '115 未连接' : system.status?.adapter === 'fake' ? '测试适配器' : '115 已配置' }}</span><ChevronRight :size="14" />
          </button>
        </template>
      </v-navigation-drawer>
      <v-app-bar flat height="64" border="b">
        <v-btn icon aria-label="展开导航" title="展开导航" class="ml-2" @click="drawer = !drawer"><Menu :size="20" /></v-btn>
        <div class="breadcrumb"><span>工作空间</span><ChevronRight :size="14" /><strong>{{ title }}</strong></div>
        <v-spacer />
        <span class="connection"><i :class="{ disconnected: !system.connected }" />{{ system.connected ? '已连接' : '连接中断' }}</span>
        <v-btn v-if="system.status?.role !== 'readonly'" icon :title="system.status?.paused ? '恢复任务' : '暂停任务'" :aria-label="system.status?.paused ? '恢复任务' : '暂停任务'" :disabled="system.busy" @click="control"><component :is="system.status?.paused ? Play : Pause" :size="18" /></v-btn>
        <v-btn icon title="刷新当前页面" aria-label="刷新当前页面" @click="system.refreshPage()"><RefreshCw :size="18" /></v-btn>
      </v-app-bar>
      <v-main>
        <div v-if="system.status?.adapter === 'fake'" class="preview-banner">模拟预览 · 非真实处理结果</div>
        <div v-if="system.status?.paused" class="pause-banner"><Pause :size="16" />任务系统已暂停</div>
        <RouterView />
      </v-main>
      <v-snackbar :model-value="!!system.error" color="error" :timeout="6000" @update:model-value="!$event && (system.error = '')">{{ system.error }}</v-snackbar>
  </v-app>
</template>
