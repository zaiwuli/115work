<script setup lang="ts">
import { useSystem } from './store'
import { date, stageLabel, textLabel } from './api'
const system = useSystem()
</script>

<template>
  <section class="data-section">
    <header class="section-heading"><h2>最近事件</h2></header>
    <div v-if="!system.status?.logs.length" class="empty small">暂无事件记录</div>
    <div v-for="event in system.status?.logs" :key="event.id" class="event-line">
      <span class="event-dot" />
      <div class="flex-content"><strong>{{ stageLabel(event.stage) }}</strong><small>{{ textLabel(event.message) }}</small></div>
      <time>{{ date(event.created_at) }}</time>
    </div>
  </section>
</template>
