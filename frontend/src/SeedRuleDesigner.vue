<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from './api'
import { X, Save } from 'lucide-vue-next'
const props = defineProps<{ rule?: any; initialName?: string }>()
const emit = defineEmits<{ close: []; saved: []; sourceChange: [] }>()
const saved = props.rule ? { name: props.rule.name, ...props.rule.config.seed_cleanup, minimum: props.rule.config.seed_cleanup?.minimum == null ? null : props.rule.config.seed_cleanup.minimum / 1048576, maximum: props.rule.config.seed_cleanup?.maximum == null ? null : props.rule.config.seed_cleanup.maximum / 1048576 } : (() => { try { return JSON.parse(localStorage.getItem('115work-seed-rule-draft') || '{}') } catch { return {} } })()
const name = ref(saved.name || props.initialName || '种子默认规则')
const mode = ref(saved.mode || 'exclude')
const extensions = ref<string[]>(saved.extensions || [])
const keywords = ref<string[]>(saved.keywords || [])
const extensionText = computed({get: () => extensions.value.join(', '), set: (value: string) => { extensions.value = [...new Set(value.split(/[,，;；\s]+/).filter(Boolean).map(x => (x.startsWith('.') ? x : '.' + x).toLowerCase()))] }})
const keywordText = computed({get: () => keywords.value.join(', '), set: (value: string) => { keywords.value = value.split(/[,，;；\n]+/).map(x => x.trim()).filter(Boolean) }})
const scope = ref(saved.scope || 'path')
const minimum = ref<number | null>(saved.minimum ?? null)
const maximum = ref<number | null>(saved.maximum ?? null)
const dryRun = ref(saved.dry_run ?? true)
const emptyDirs = ref(saved.empty_dirs ?? true)
const autoExtract = ref(saved.auto_extract ?? false)
const feedback = ref('')
const valid = computed(() => name.value.trim() && (mode.value !== 'include' || extensions.value.length > 0) && (minimum.value == null || minimum.value >= 0) && (maximum.value == null || maximum.value >= 0) && (minimum.value == null || maximum.value == null || minimum.value <= maximum.value))
const steps = computed(() => [
  mode.value === 'all' ? '不按文件类型清理' : mode.value === 'include' ? `仅保留 ${extensions.value.join('、') || '指定类型'} 文件` : `排除 ${extensions.value.join('、') || '指定类型（尚未设置）'} 文件`,
  ...(keywords.value.length ? [`排除${scope.value === 'path' ? '完整路径' : '文件名'}包含 ${keywords.value.join('、')} 的文件`] : []),
  ...(minimum.value != null || maximum.value != null ? [`保留 ${minimum.value ?? 0} MiB 至 ${maximum.value ?? '不限'} MiB 的文件`] : []),
  '所选文件全部完成后，提示种子下载成功',
  emptyDirs.value ? '清理空子目录，保留种子根目录' : '保留空目录',
])
async function save() {
  try {
    const config = { pattern: '*', seed_cleanup: { enabled: true, mode: mode.value, extensions: extensions.value, keywords: keywords.value, scope: scope.value, minimum: minimum.value == null || minimum.value === ('' as any) ? null : Math.round(minimum.value * 1048576), maximum: maximum.value == null || maximum.value === ('' as any) ? null : Math.round(maximum.value * 1048576), empty_dirs: emptyDirs.value, auto_extract: autoExtract.value, dry_run: dryRun.value } }
    await api('/api/v1/rules' + (props.rule ? '/' + props.rule.id : ''), {name: name.value, source_id: '115-torrent', config, enabled: true, ...(props.rule ? {expected_revision: props.rule.revision} : {})}, props.rule ? 'PUT' : 'POST')
    feedback.value = '规则已保存，只应用于之后上传的种子。'
    emit('saved')
  } catch (e) { feedback.value = (e as Error).message }
}

</script>

<template>
  <v-card>
    <header class="dialog-heading"><h2>{{ props.rule ? '编辑' : '添加' }}种子下载规则</h2><v-btn icon variant="text" aria-label="关闭" @click="emit('close')"><X :size="20" /></v-btn></header>
    <v-card-text>
      <v-form class="dialog-form" @submit.prevent="save">
        <v-text-field v-model="name" label="规则名称" maxlength="100" />
        <v-select model-value="115-torrent" label="来源" :items="[{title:'种子下载',value:'115-torrent'},{title:'所有普通来源',value:''}]" @update:model-value="$event !== '115-torrent' && emit('sourceChange')" />
        <h3>下载后清理</h3>
        <p class="rule-help">完整下载后才清理，仅作用于本次种子的资源目录。普通磁链和其他来源不使用此规则。</p>
        <v-select v-model="mode" label="文件类型处理" :items="[{title:'不按类型清理',value:'all'},{title:'删除指定类型',value:'exclude'},{title:'仅保留指定类型',value:'include'}]" />
        <v-text-field v-if="mode !== 'all'" v-model="extensionText" :label="mode === 'include' ? '保留的扩展名' : '删除的扩展名'" placeholder=".mp4, .mkv, .avi, .mov, .wmv, .ts" hint="直接输入，多个扩展名用逗号或空格分隔；不区分大小写。" persistent-hint />
        <v-text-field v-model="keywordText" label="删除关键词" placeholder="广告, 推广, 扫码" hint="多个关键词用逗号分隔，命中任意一项即删除，包括已列入保留类型的文件。" persistent-hint />
        <v-select v-model="scope" label="关键词匹配范围" :items="[{title:'完整相对路径（含文件夹）',value:'path'},{title:'仅文件名',value:'name'}]" />
        <h3>文件大小保留范围</h3>
        <p class="rule-help">按单个文件判断，不是整个种子的总大小。两项留空表示不按大小删除；等于边界的文件保留。</p>
        <div class="form-grid"><v-text-field v-model.number="minimum" type="number" min="0" clearable label="删除小于此大小的文件 / MiB" placeholder="不限" /><v-text-field v-model.number="maximum" type="number" min="0" clearable label="删除大于此大小的文件 / MiB" placeholder="不限" /></div>
        <p class="rule-help">例如只想删除小于 10 MiB 的文件：第一项填 10，第二项留空。1 GiB = 1024 MiB。类型、关键词、大小任意删除条件命中都会删除；仅保留视频时字幕和压缩包也会被删除。</p>
        <h3>压缩包处理</h3><v-switch v-model="autoExtract" label="自动解压保留下来的压缩包（默认关闭）" color="primary" hide-details /><p class="rule-help">清理完成后，压缩包直接解压到它所在的云端目录；保留原包，不自动转本地。预演模式不启动解压。被清理规则删除的压缩包不解压。</p><h3>执行方式</h3>
        <v-switch v-model="dryRun" label="仅预演，不删除文件" color="primary" hide-details />
        <v-switch v-model="emptyDirs" label="删除空子目录（保留种子根目录）" color="primary" hide-details />
        <p class="rule-help">默认预演。关闭预演后真实删除文件。规则修改只影响新上传的种子。</p>
        <details class="rule-summary"><summary>查看执行预览</summary><ol><li v-for="step in steps" :key="step">{{ step }}</li></ol></details>
        <p v-if="feedback" role="status">{{ feedback }}</p>
        <div class="seed-actions"><v-btn variant="text" @click="emit('close')">取消</v-btn><v-btn type="submit" color="primary" :disabled="!valid"><Save :size="17" class="mr-2" />保存规则</v-btn></div>
      </v-form>
    </v-card-text>
  </v-card>
</template>

<style scoped>
.rule-help { font-size: 13px; line-height: 1.8; color: rgb(var(--v-theme-on-surface)); opacity: .7; margin: 0 0 12px; }
.rule-summary { font-size: 14px; line-height: 1.8; }
.rule-summary ol { padding-left: 24px; margin-top: 12px; }
.seed-actions { display: flex; justify-content: flex-end; gap: 12px; }
</style>
