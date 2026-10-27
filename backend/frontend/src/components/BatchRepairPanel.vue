<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { CheckCheck, Play, RefreshCw } from '@lucide/vue'
import type { IssueView } from '../types'

const props = defineProps<{
  issues: IssueView[]
  loading: boolean
  submitting: boolean
  message: string
}>()

const emit = defineEmits<{
  submit: [payload: { issueIds: string[]; runKey: string }]
  refresh: []
}>()

const selected = ref<string[]>([])
const runKey = ref('')
const allSelected = computed(() => (
  props.issues.length > 0 && props.issues.every((issue) => selected.value.includes(issue.id))
))

watch(() => props.issues, (issues) => {
  const available = new Set(issues.map((issue) => issue.id))
  selected.value = selected.value.filter((id) => available.has(id))
})

function toggleAll(): void {
  selected.value = allSelected.value ? [] : props.issues.map((issue) => issue.id)
}

function submit(): void {
  if (!selected.value.length || props.submitting) return
  emit('submit', { issueIds: [...selected.value], runKey: runKey.value })
}
</script>

<template>
  <section class="panel batch-panel">
    <header class="panel-header batch-header">
      <div>
        <h2>批量创建修复任务</h2>
        <p>选择尚未解决且当前没有运行任务的 SWE-bench Issue</p>
      </div>
      <div class="batch-actions">
        <input v-model="runKey" aria-label="批次标识" placeholder="批次标识（可选）" />
        <button class="icon-button" title="刷新待修复列表" :disabled="loading" @click="$emit('refresh')">
          <RefreshCw :size="16" :class="{ spinning: loading }" />
        </button>
        <button class="button secondary-button" :disabled="!issues.length" @click="toggleAll">
          <CheckCheck :size="16" />{{ allSelected ? '取消全选' : '全选' }}
        </button>
        <button class="button primary-button" :disabled="!selected.length || submitting" @click="submit">
          <Play :size="15" />{{ submitting ? '提交中' : `创建 ${selected.length} 个任务` }}
        </button>
      </div>
    </header>

    <div v-if="message" class="batch-message">{{ message }}</div>
    <div v-if="loading && !issues.length" class="batch-loading">正在加载待修复 Issue...</div>
    <div v-else-if="!issues.length" class="batch-empty">当前没有可批量提交的 SWE-bench Issue</div>
    <div v-else class="batch-list">
      <label v-for="issue in issues" :key="issue.id" class="batch-row">
        <input v-model="selected" type="checkbox" :value="issue.id" />
        <span class="batch-repository">
          <strong>{{ issue.repository ? `${issue.repository.ownerLogin}/${issue.repository.name}` : '加载中...' }}</strong>
          <small>{{ issue.externalId || issue.title }}</small>
        </span>
        <span class="batch-title">{{ issue.body }}</span>
        <code>{{ issue.baseSha?.slice(0, 12) }}</code>
      </label>
    </div>
  </section>
</template>
