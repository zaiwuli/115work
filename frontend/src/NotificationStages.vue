<script setup lang="ts">
import { computed, ref } from 'vue'
type Stage = { id: string; label: string; group: string; group_label: string; result: boolean; attention: boolean }
const props = defineProps<{ modelValue: Record<string, boolean>; catalog: Stage[]; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: Record<string, boolean>] }>()
const groups = computed(() => [...new Set(props.catalog.map(x => x.group))].map(key => ({ key, title: props.catalog.find(x => x.group === key)!.group_label, stages: props.catalog.filter(x => x.group === key) })))
const selected = ref('')
const preview = computed(() => props.catalog.find(x => x.id === selected.value) || props.catalog[0])
function setStages(stages: Stage[], enabled: boolean) {
  emit('update:modelValue', { ...props.modelValue, ...Object.fromEntries(stages.map(x => [x.id, enabled])) })
}
function preset(mode: string) {
  emit('update:modelValue', Object.fromEntries(props.catalog.map(x => [x.id, mode === 'all' || (mode === 'result' ? x.result : x.attention)])))
}
</script>

<template>
  <div class="notification-stages">
    <h3>通知阶段</h3>
    <p class="muted">各阶段独立控制，失败通知也遵循开关。同一次执行的同一阶段只通知一次，手动重试可再次通知。</p>
    <div class="d-flex flex-wrap ga-2 mb-4">
      <v-btn v-for="item in [{ key: 'result', title: '仅结果' }, { key: 'attention', title: '仅异常' }, { key: 'all', title: '全部阶段' }]" :key="item.key" size="small" variant="outlined" :disabled="disabled" @click="preset(item.key)">{{ item.title }}</v-btn>
    </div>
    <v-expansion-panels variant="accordion" multiple>
      <v-expansion-panel v-for="group in groups" :key="group.key">
        <v-expansion-panel-title>{{ group.title }}<span class="ml-3 muted">{{ group.stages.filter(x => modelValue?.[x.id]).length }} / {{ group.stages.length }}</span></v-expansion-panel-title>
        <v-expansion-panel-text>
          <div class="d-flex ga-2"><v-btn size="small" variant="text" :disabled="disabled" @click="setStages(group.stages, true)">全选</v-btn><v-btn size="small" variant="text" :disabled="disabled" @click="setStages(group.stages, false)">全不选</v-btn></div>
          <div class="stage-grid"><v-checkbox v-for="stage in group.stages" :key="stage.id" :model-value="!!modelValue?.[stage.id]" :label="stage.label" :disabled="disabled" color="primary" hide-details @update:model-value="setStages([stage], !!$event)" /></div>
        </v-expansion-panel-text>
      </v-expansion-panel>
    </v-expansion-panels>
    <h3 class="mt-5 mb-3">阶段预览</h3>
    <v-select v-model="selected" :items="catalog" item-title="label" item-value="id" label="选择通知阶段" />
    <p v-if="preview" class="mb-4">{{ preview.group_label }} · {{ preview.label }}：{{ disabled ? '通知总开关已关闭' : modelValue?.[preview.id] ? '会发送通知' : '不会发送通知' }}。预览不会实际发送。</p>
  </div>
</template>

<style scoped>
.notification-stages { margin: 20px 0; }
.notification-stages p { margin: 8px 0 16px; }
.stage-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
</style>
