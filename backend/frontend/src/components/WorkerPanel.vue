<script setup lang="ts">
import { Bot, Radio, Timer } from '@lucide/vue'
import { computed } from 'vue'
import type { MonitoringWorker } from '../types'
import { relativeTime, shortId } from '../format'

const props = defineProps<{
  workers: MonitoringWorker[]
  waitingByStage: Partial<Record<string, number>>
  presenceAvailable: boolean
  loading: boolean
}>()
defineEmits<{ select: [taskId: string] }>()

const activeAgents = computed(() => props.workers.filter(
  (worker) => worker.availability === 'ONLINE' && worker.activeTaskId,
))

const pipelineRoles = [
  { key: 'COORDINATOR', label: 'Coordinator', workerToken: 'coordinator' },
  { key: 'IMPLEMENT', label: 'Implement Worker', workerToken: 'implement' },
  { key: 'VALIDATION', label: 'Validation Worker', workerToken: 'validation' },
  { key: 'FAILURE_REVIEW', label: 'Failure Review', workerToken: 'failure-review' },
  { key: 'CORRECTIVE_IMPLEMENT', label: 'Corrective Implement', workerToken: 'corrective-implement' },
  { key: 'EXPERIENCE_MEMORY', label: 'Experience Memory', workerToken: 'experience-memory' },
] as const

// 较长的角色名优先，避免 corrective-implement 被 implement 的子串误匹配。
const workerRoleMatchers = [...pipelineRoles].sort(
  (left, right) => right.workerToken.length - left.workerToken.length,
)

const stageRoles: Record<string, string> = {
  ANALYZING: 'COORDINATOR',
  COORDINATOR: 'COORDINATOR',
  IMPLEMENT: 'IMPLEMENT',
  VALIDATING: 'VALIDATION',
  VALIDATION: 'VALIDATION',
  FAILURE_REVIEW: 'FAILURE_REVIEW',
  RETRYING: 'FAILURE_REVIEW',
  CORRECTIVE_IMPLEMENT: 'CORRECTIVE_IMPLEMENT',
  EXPERIENCE_MEMORY: 'EXPERIENCE_MEMORY',
}

function roleKey(worker: MonitoringWorker): string | null {
  if (worker.workerRole) {
    const role = worker.workerRole.toUpperCase().replace(/-/g, '_')
    if (pipelineRoles.some((candidate) => candidate.key === role)) return role
  }
  const stage = worker.currentStage?.toUpperCase()
  if (stage && stageRoles[stage]) return stageRoles[stage]
  const workerId = worker.workerId.toLowerCase()
  return workerRoleMatchers.find((role) => workerId.includes(`-${role.workerToken}-`))?.key || null
}

const rolePools = computed(() => pipelineRoles.map((role) => {
  const workers = props.workers.filter((worker) => roleKey(worker) === role.key)
  const onlineWorkers = workers.filter((worker) => worker.availability === 'ONLINE')
  const activeWorkers = onlineWorkers.filter((worker) => Boolean(worker.activeTaskId))
  return {
    ...role,
    onlineCount: onlineWorkers.length,
    activeWorkers,
    waitingCount: props.waitingByStage[role.key] || 0,
  }
}))
</script>

<template>
  <section class="panel worker-panel">
    <header class="panel-header">
      <div>
        <h2>Agent Worker 池</h2>
        <p>每类角色最多 3 个实例，显示执行任务与阶段队列等待量</p>
      </div>
      <span class="section-count">{{ activeAgents.length }}</span>
    </header>

    <div v-if="loading && !workers.length" class="skeleton-list">
      <div v-for="item in 3" :key="item" class="skeleton-row" />
    </div>
    <div v-else class="worker-list">
      <section v-for="pool in rolePools" :key="pool.key" class="worker-row agent-pool">
        <span class="worker-avatar"><Bot :size="18" /></span>
        <span class="worker-main">
          <span class="worker-title">
            <strong>{{ pool.label }}</strong>
            <span :class="pool.activeWorkers.length ? 'agent-running' : 'agent-idle'">
              <span />{{ pool.activeWorkers.length ? `运行 ${pool.activeWorkers.length}/${pool.onlineCount}` : `空闲 ${pool.onlineCount}` }}
            </span>
          </span>
          <span class="pool-summary">
            {{ presenceAvailable ? `在线 ${pool.onlineCount}` : '在线状态暂不可用' }} · {{ pool.waitingCount ? `队列等待 ${pool.waitingCount}` : '队列无等待' }}
          </span>
          <template v-if="pool.activeWorkers.length">
            <button
              v-for="worker in pool.activeWorkers"
              :key="worker.workerId"
              class="pool-task"
              @click="worker.activeTaskId && $emit('select', worker.activeTaskId)"
            >
              <span class="worker-task">
                <b>{{ worker.repositoryName || '未知仓库' }}</b>
                <span>{{ worker.issueTitle || `任务 ${shortId(worker.activeTaskId)}` }}</span>
              </span>
              <span v-if="worker.currentMessage" class="worker-detail">{{ worker.currentMessage }}</span>
              <span class="worker-meta">
                <span><Radio :size="13" />任务 {{ shortId(worker.activeTaskId) }}</span>
                <span><Timer :size="13" />{{ relativeTime(worker.lastHeartbeatAt) }}</span>
              </span>
            </button>
          </template>
          <span v-else class="worker-detail">当前没有被该角色领取的任务。</span>
        </span>
      </section>
    </div>
  </section>
</template>
