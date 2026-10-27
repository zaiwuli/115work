<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from './api'
const props = defineProps<{ rule?: any }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const saved = props.rule ? { name: props.rule.name, ...props.rule.config.seed_cleanup, minimum: props.rule.config.seed_cleanup?.minimum == null ? null : props.rule.config.seed_cleanup.minimum / 1048576, maximum: props.rule.config.seed_cleanup?.maximum == null ? null : props.rule.config.seed_cleanup.maximum / 1048576 } : (() => { try { return JSON.parse(localStorage.getItem('115work-seed-rule-draft') || '{}') } catch { return {} } })()
const name = ref(saved.name || '种子默认规则')
const mode = ref(saved.mode || 'exclude')
const extensions = ref<string[]>(saved.extensions || [])
const keywords = ref<string[]>(saved.keywords || [])
const scope = ref(saved.scope || 'path')
const minimum = ref<number | null>(saved.minimum ?? null)
const maximum = ref<number | null>(saved.maximum ?? null)
const dryRun = ref(saved.dry_run ?? true)
const emptyDirs = ref(saved.empty_dirs ?? true)
const feedback = ref('')
const valid = computed(() => name.value.trim() && (minimum.value == null || minimum.value >= 0) && (maximum.value == null || maximum.value >= 0) && (minimum.value == null || maximum.value == null || minimum.value <= maximum.value))
const steps = computed(() => [
  mode.value === 'all' ? '不按文件类型清理' : mode.value === 'include' ? `仅保留 ${extensions.value.join('、') || '指定类型'} 文件` : `排除 ${extensions.value.join('、') || '指定类型（尚未设置）'} 文件`,
  ...(keywords.value.length ? [`排除${scope.value === 'path' ? '完整路径' : '文件名'}包含 ${keywords.value.join('、')} 的文件`] : []),
  ...(minimum.value != null || maximum.value != null ? [`保留 ${minimum.value ?? 0} MiB 至 ${maximum.value ?? '不限'} MiB 的文件`] : []),
  '所选文件全部完成后，提示种子下载成功',
  emptyDirs.value ? '清理空子目录，保留种子根目录' : '保留空目录',
])
async function save() {
  try {
    const config = { pattern: '*', seed_cleanup: { enabled: true, mode: mode.value, extensions: extensions.value, keywords: keywords.value, scope: scope.value, minimum: minimum.value == null || minimum.value === ('' as any) ? null : Math.round(minimum.value * 1048576), maximum: maximum.value == null || maximum.value === ('' as any) ? null : Math.round(maximum.value * 1048576), empty_dirs: emptyDirs.value, dry_run: dryRun.value } }
    await api('/api/v1/rules' + (props.rule ? '/' + props.rule.id : ''), {name: name.value, source_id: '115-torrent', config, enabled: true, ...(props.rule ? {expected_revision: props.rule.revision} : {})}, props.rule ? 'PUT' : 'POST')
    feedback.value = '规则已保存，只应用于之后上传的种子。'
    emit('saved')
  } catch (e) { feedback.value = (e as Error).message }
}

</script>

<template>
  <v-card class="seed-designer">
    <header class="seed-heading"><div><div class="eyebrow">处理规则 / 种子下载</div><h2>完整下载，再按规则清理</h2><p>模板只作用于本次种子的资源目录，下载完成后执行。</p></div><v-btn variant="text" @click="emit('close')">关闭</v-btn></header>
    <div class="seed-notice" role="status">下载后清理 · 默认仅预演。关闭预演后将真实删除匹配文件；不会删除种子根目录。</div>
    <div class="seed-layout">
      <v-form class="seed-fields" @submit.prevent="save">
        <section><span class="seed-number">01</span><h3>基本信息</h3><p>适用来源固定为种子下载，匹配上传后的资源名称。</p><v-text-field v-model="name" label="规则名称" maxlength="100" /><v-text-field model-value="种子下载" label="适用来源" readonly /><div class="seed-detail">默认模板覆盖每次种子上传；后续修改只影响新任务。</div></section>
        <section><span class="seed-number">02</span><h3>下载后清理</h3><p>先完整下载；类型、关键词、大小任意删除条件命中即可清理。</p><v-select v-model="mode" label="文件类型模式" :items="[{title:'不按类型清理',value:'all'},{title:'清理指定类型',value:'exclude'},{title:'仅保留指定类型',value:'include'}]" /><v-combobox v-if="mode !== 'all'" v-model="extensions" label="扩展名" :items="['.url','.lnk','.exe','.jpg','.png','.mp4','.mkv','.zip','.rar']" multiple chips closable-chips hint="输入扩展名后按回车，例如 .url" persistent-hint /><v-combobox v-model="keywords" label="排除关键词" multiple chips closable-chips hint="任一关键词命中即排除；忽略大小写" persistent-hint /><v-select v-model="scope" label="关键词匹配范围" :items="[{title:'完整相对路径（含文件夹）',value:'path'},{title:'仅文件名',value:'name'}]" /><div class="seed-size"><v-text-field v-model.number="minimum" type="number" min="0" clearable label="最小文件 / MiB" placeholder="不限" /><v-text-field v-model.number="maximum" type="number" min="0" clearable label="最大文件 / MiB" placeholder="不限" /></div><div class="seed-detail">大小为保留范围；范围外文件将清理。仅保留视频会删除压缩包和字幕，请先预演。</div></section>
        <section><span class="seed-number">03</span><h3>执行方式</h3><v-switch v-model="dryRun" label="仅预演，不删除文件" color="primary" /><v-switch v-model="emptyDirs" label="清理空子目录" color="primary" /><p>仅在115确认下载完成后运行；普通文件不进入解压任务队列。原地解压本版暂未启用。</p></section>
        <footer class="seed-footer"><span v-if="feedback" role="status">{{ feedback }}</span><v-btn type="submit" color="primary" :disabled="!valid">保存种子规则</v-btn></footer>
      </v-form>
      <aside class="seed-preview"><div class="eyebrow">执行预览</div><h3>{{ name || '未命名规则' }}</h3><span class="seed-badge">种子下载</span><ol><li v-for="step in steps" :key="step">{{ step }}</li></ol><div class="seed-detail">下载成功与解压结果分别记录。解压失败不会改变已完成的下载结果。</div><hr /><h4>执行边界</h4><p>不使用 wanted 接口。下载完整资源后再清理，记录匹配清单、删除结果和失败原因。</p></aside>
    </div>
  </v-card>
</template>

<style scoped>
.seed-designer{--seed-accent:#167665;--seed-line:#dce7e1;background:#f7faf7;color:#243d35}.seed-heading{display:flex;justify-content:space-between;gap:24px;padding:30px 32px 20px;background:linear-gradient(120deg,#edf5ed,#f8faf5)}.seed-heading h2{font-size:24px;margin:8px 0}.seed-heading p,.seed-fields section>p{color:#65776e;font-size:14px;margin:8px 0 20px}.seed-notice{padding:12px 32px;background:#fff4dc;color:#71501c;font-size:13px}.seed-layout{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:28px;padding:28px 32px}.seed-fields section{position:relative;border-bottom:1px solid var(--seed-line);padding:0 0 22px;margin-bottom:26px}.seed-fields h3{font-size:18px;margin:0 0 16px}.seed-number{float:right;color:var(--seed-accent);font-family:Georgia,serif;font-size:25px}.seed-fields :deep(.v-input){margin-bottom:12px}.seed-size{display:grid;grid-template-columns:1fr 1fr;gap:14px}.seed-detail{padding:12px 14px;background:#edf3ed;border-radius:8px;font-size:13px;line-height:1.7;color:#4c6659}.seed-preview{align-self:start;position:sticky;top:12px;background:#fff;border:1px solid var(--seed-line);padding:24px;border-radius:12px;box-shadow:0 10px 30px #284c3610}.seed-preview h3{margin:12px 0}.seed-preview ol{padding-left:22px;margin:24px 0}.seed-preview li{padding-left:6px;margin-bottom:18px;line-height:1.7;font-size:14px}.seed-badge{display:inline-block;padding:4px 10px;border-radius:20px;background:#e5f1e9;color:var(--seed-accent);font-size:12px}.seed-preview hr{border:0;border-top:1px solid var(--seed-line);margin:22px 0}.seed-preview p{font-size:13px;line-height:1.8;margin-top:8px;color:#65776e}.seed-facts div{display:flex;justify-content:space-between;gap:16px;padding:10px 0;border-bottom:1px solid var(--seed-line);font-size:13px}.seed-facts dt{color:#65776e}.seed-footer{display:flex;align-items:center;justify-content:flex-end;gap:16px}.seed-footer span{font-size:13px;color:var(--seed-accent)}@media(max-width:760px){.seed-layout{grid-template-columns:1fr;padding:20px}.seed-heading{padding:20px}.seed-heading h2{font-size:20px}.seed-notice{padding:12px 20px}.seed-preview{position:static;grid-row:1}.seed-size{grid-template-columns:1fr}.seed-footer{flex-wrap:wrap}}
</style>
