<script setup lang="ts">
import { useSystem } from './store'
const system = useSystem()
</script>

<template>
  <section class="settings-section">
    <h2>运行状态</h2>
    <dl class="health-list">
      <div><dt>调度状态</dt><dd>{{ system.status?.paused ? '已暂停' : '运行中' }}</dd></div>
      <div><dt>115 连接</dt><dd>{{ system.status?.adapter === 'disabled' ? '未配置' : system.status?.adapter === 'fake' ? '测试模式' : '已配置' }}</dd></div>
      <div><dt>来源目录</dt><dd>{{ system.status?.sources.length || 0 }} 个</dd></div>
      <div><dt>本地执行器</dt><dd>{{ system.status?.local_formats || '-' }}</dd></div>
      <div><dt>应用版本</dt><dd>{{ system.status?.version || '-' }}</dd></div>
      <div><dt>构建编号</dt><dd class="path-cell">{{ system.status?.build_revision || '-' }}</dd></div>
      <div><dt>构建时间</dt><dd>{{ system.status?.build_time || '-' }}</dd></div>
    </dl>
  </section>
</template>
