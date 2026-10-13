<script setup lang="ts">
import { useRoute } from 'vue-router'
const route = useRoute()
const sections = [
  { key: 'connections', title: '连接管理', to: '/settings/connections' },
  { key: 'cloud', title: '云目录', to: '/settings?section=cloud' },
  { key: 'notifications', title: '通知', to: '/settings?section=notifications' },
  { key: 'scheduler', title: '调度', to: '/settings?section=scheduler' },
  { key: 'status', title: '运行状态', to: '/settings?section=status' },
]
</script>

<template>
  <nav class="section-nav" aria-label="系统设置分页">
    <RouterLink v-for="section in sections" :key="section.key" :to="section.to"
      :class="{ active: (route.meta.view === 'connections' ? 'connections' : route.query.section || 'cloud') === section.key }"
      :aria-current="(route.meta.view === 'connections' ? 'connections' : route.query.section || 'cloud') === section.key ? 'page' : undefined">
      {{ section.title }}
    </RouterLink>
  </nav>
</template>
