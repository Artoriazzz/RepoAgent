<script setup lang="ts">
import { ChevronLeft, ChevronRight, ExternalLink, Trash2 } from '@lucide/vue'
import type { TaskStatus, TaskView } from '../types'
import { formatDuration, formatNumber, formatTime, shortId } from '../format'
import StatusBadge from './StatusBadge.vue'

defineProps<{
  tasks: TaskView[]
  loading: boolean
  page: number
  totalPages: number
  totalElements: number
  status: TaskStatus | ''
  clearing: boolean
}>()

defineEmits<{
  select: [task: TaskView]
  delete: [task: TaskView]
  clear: []
  page: [page: number]
  'update:status': [status: TaskStatus | '']
}>()

const statuses: Array<{ value: TaskStatus | ''; label: string }> = [
  { value: '', label: '全部状态' }, { value: 'QUEUED', label: '排队中' },
  { value: 'ANALYZING', label: '分析中' }, { value: 'FIXING', label: '修复中' },
  { value: 'VALIDATING', label: '验证中' }, { value: 'PATCH_READY', label: '补丁就绪' },
  { value: 'FAILED', label: '失败' }, { value: 'CANCELLED', label: '已取消' },
]
</script>

<template>
  <section class="panel task-panel">
    <header class="panel-header task-header">
      <div>
        <h2>修复任务</h2>
        <p>按创建时间倒序，共 {{ totalElements }} 条</p>
      </div>
      <div class="task-header-actions">
        <select :value="status" aria-label="筛选任务状态" @change="$emit('update:status', ($event.target as HTMLSelectElement).value as TaskStatus | '')">
          <option v-for="option in statuses" :key="option.value" :value="option.value">{{ option.label }}</option>
        </select>
        <button class="button danger-button" :disabled="clearing || !totalElements" @click="$emit('clear')">
          <Trash2 :size="15" />{{ clearing ? '清空中...' : '清空修复记录' }}
        </button>
      </div>
    </header>

    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>任务</th><th>仓库 / Issue</th><th>执行阶段</th><th>最终结果</th><th>Worker</th><th>耗时</th><th>Token</th><th>创建时间</th><th /></tr>
        </thead>
        <tbody>
          <tr v-if="loading && !tasks.length" v-for="item in 5" :key="item" class="loading-row">
            <td colspan="9"><span /></td>
          </tr>
          <tr v-else-if="!tasks.length">
            <td colspan="9" class="table-empty">当前筛选条件下没有任务</td>
          </tr>
          <tr v-for="task in tasks" v-else :key="task.id" class="task-row" @click="$emit('select', task)">
            <td><strong class="mono">{{ shortId(task.id) }}</strong><small>第 {{ task.attemptNumber }} 次</small></td>
            <td><strong>{{ task.repository ? `${task.repository.ownerLogin}/${task.repository.name}` : '加载中...' }}</strong><small>{{ task.issue?.title || shortId(task.issueId) }}</small></td>
            <td>
              <StatusBadge :status="task.resultStatus === 'PENDING' ? (task.currentStage || task.status) : 'COMPLETED'" />
              <small v-if="task.currentMessage">{{ task.currentMessage }}</small>
              <small v-if="task.correctionAttempt">纠正 {{ task.correctionAttempt }}/{{ task.maxCorrectionAttempts }}</small>
            </td>
            <td><StatusBadge :status="task.resultStatus" /></td>
            <td><span class="mono compact">{{ task.workerId || '--' }}</span></td>
            <td>{{ formatDuration(task.durationMs) }}</td>
            <td>{{ formatNumber(task.totalTokens) }}</td>
            <td>{{ formatTime(task.createdAt) }}</td>
            <td>
              <div class="row-actions">
                <button class="icon-button" title="查看任务详情" @click.stop="$emit('select', task)"><ExternalLink :size="16" /></button>
                <button v-if="task.resultStatus !== 'PENDING'" class="icon-button delete-icon-button" title="删除修复记录" @click.stop="$emit('delete', task)"><Trash2 :size="16" /></button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <footer class="pagination">
      <span>第 {{ totalPages ? page + 1 : 0 }} / {{ totalPages }} 页</span>
      <div>
        <button class="icon-button" title="上一页" :disabled="page <= 0" @click="$emit('page', page - 1)"><ChevronLeft :size="18" /></button>
        <button class="icon-button" title="下一页" :disabled="page + 1 >= totalPages" @click="$emit('page', page + 1)"><ChevronRight :size="18" /></button>
      </div>
    </footer>
  </section>
</template>
