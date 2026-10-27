<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Activity, CheckCircle2, Clock3, ListTodo, RefreshCw, Server, Settings, TriangleAlert, Wifi } from '@lucide/vue'
import { api, getUserId, saveUserId } from './api'
import { formatDuration, formatTime, shortId } from './format'
import type { Issue, IssueView, MonitoringOverview, MonitoringWorker, RepairAdmissionStatus, Repository, TaskStatus, TaskView } from './types'
import BatchRepairPanel from './components/BatchRepairPanel.vue'
import MetricCard from './components/MetricCard.vue'
import StatusBadge from './components/StatusBadge.vue'
import TaskDrawer from './components/TaskDrawer.vue'
import TaskTable from './components/TaskTable.vue'
import WorkerPanel from './components/WorkerPanel.vue'

const userId = ref(getUserId())
const draftUserId = ref(userId.value)
const showSettings = ref(!userId.value)
const loading = ref(false)
const refreshing = ref(false)
const error = ref('')
const lastUpdated = ref('')
const overview = ref<MonitoringOverview | null>(null)
const repairCapacity = ref<RepairAdmissionStatus | null>(null)
const workers = ref<MonitoringWorker[]>([])
const tasks = ref<TaskView[]>([])
const taskPage = ref(0)
const totalPages = ref(0)
const totalElements = ref(0)
const statusFilter = ref<TaskStatus | ''>('')
const selectedTask = ref<TaskView | null>(null)
const repairCandidates = ref<IssueView[]>([])
const candidatesLoading = ref(false)
const batchSubmitting = ref(false)
const clearingTasks = ref(false)
const batchMessage = ref('')
const distributionMode = ref<'QUEUED' | 'ACTIVE' | 'COMPLETED'>('ACTIVE')
const issueCache = new Map<string, Issue>()
const repositoryCache = new Map<string, Repository>()
let refreshTimer: number | null = null

const queuedStageNames: Record<string, string> = {
  COORDINATOR: 'Coordinator',
  IMPLEMENT: 'Implement',
  VALIDATION: 'Validation',
  FAILURE_REVIEW: 'Failure Review',
  CORRECTIVE_IMPLEMENT: 'Corrective Implement',
  EXPERIENCE_MEMORY: 'Experience Memory',
}

const queuedStages = Object.entries(queuedStageNames)

type DistributionRow = {
  status: string
  count: number
  label: string
  detail?: string
}

const statusRows = computed(() => {
  if (distributionMode.value === 'QUEUED') {
    const queued = overview.value?.byQueuedPipelineStage || {}
    const rows: DistributionRow[] = queuedStages.map(([stage, label]) => ({
      status: 'QUEUED',
      count: Number(queued[stage] || 0),
      label: `${label} 队列`,
    }))
    const maximum = Math.max(1, ...rows.map((row) => row.count))
    return rows.map((row) => ({
      ...row,
      width: `${Math.max(row.count ? 5 : 0, row.count / maximum * 100)}%`,
    }))
  }
  if (distributionMode.value === 'COMPLETED') {
    const results = overview.value?.byResultStatus || {}
    const rows: DistributionRow[] = [
      { status: 'SUCCEEDED', count: results.SUCCEEDED || 0, label: '修复成功' },
      {
        status: 'FAILED',
        count: (results.FAILED || 0) + (results.ENVIRONMENT_ERROR || 0),
        label: '修复失败',
      },
    ]
    return rows.map((row, _, allRows) => ({
      ...row,
      width: `${Math.max(row.count ? 5 : 0, row.count / Math.max(1, ...allRows.map((item) => item.count)) * 100)}%`,
    }))
  }
  const active = overview.value?.byActiveStage || {}
  const rows: DistributionRow[] = [
    { status: 'ANALYZING', count: active.ANALYZING || 0, label: '分析中' },
    { status: 'FIXING', count: active.FIXING || 0, label: '修复中' },
    { status: 'VALIDATING', count: active.VALIDATING || 0, label: '验证中' },
    { status: 'RETRYING', count: active.RETRYING || 0, label: '重试中' },
    { status: 'EXPERIENCE_MEMORY', count: active.EXPERIENCE_MEMORY || 0, label: '经验记忆中' },
    { status: 'PATCH_READY', count: active.PATCH_READY || 0, label: '补丁就绪' },
  ]
  const maximum = Math.max(1, ...rows.map((row) => row.count))
  return rows.map((row) => ({
    ...row,
    width: `${Math.max(row.count ? 5 : 0, row.count / maximum * 100)}%`,
  }))
})

const distributionTotal = computed(() => statusRows.value.reduce((sum, row) => sum + row.count, 0))
const distributionDescription = computed(() => {
  if (distributionMode.value === 'QUEUED') return '查看每类 Agent 当前等待领取的任务'
  if (distributionMode.value === 'ACTIVE') return '查看修复任务当前进行到哪一步'
  return '查看最终修复成功与失败数量'
})

watch(statusFilter, () => {
  taskPage.value = 0
  void loadData(false)
})

watch(taskPage, () => void loadTasks())

onMounted(() => {
  if (userId.value) void loadData(true)
  refreshTimer = window.setInterval(() => {
    if (document.visibilityState === 'visible' && userId.value) void loadData(false)
  }, 5_000)
})

onBeforeUnmount(() => {
  if (refreshTimer) window.clearInterval(refreshTimer)
})

async function loadData(initial: boolean): Promise<void> {
  if (!userId.value) return
  if (initial) loading.value = true
  else refreshing.value = true
  error.value = ''
  try {
    const [overviewResult, workerResult, capacityResult] = await Promise.all([api.overview(), api.workers(), api.repairCapacity()])
    overview.value = overviewResult
    workers.value = workerResult
    repairCapacity.value = capacityResult
    await loadTasks()
    if (initial) await loadRepairCandidates()
    lastUpdated.value = new Date().toISOString()
  } catch (cause) {
    error.value = friendlyError(cause)
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

async function loadRepairCandidates(): Promise<void> {
  if (!userId.value) return
  candidatesLoading.value = true
  try {
    const page = await api.repairableIssues(0, 50)
    repairCandidates.value = await Promise.all(page.content.map(enrichIssue))
  } catch (cause) {
    error.value = friendlyError(cause)
  } finally {
    candidatesLoading.value = false
  }
}

async function enrichIssue(issue: Issue): Promise<IssueView> {
  let repository = repositoryCache.get(issue.repositoryId)
  if (!repository) {
    repository = await api.repository(issue.repositoryId)
    repositoryCache.set(repository.id, repository)
  }
  return { ...issue, repository }
}

async function submitRepairBatch(payload: { issueIds: string[]; runKey: string }): Promise<void> {
  batchSubmitting.value = true
  batchMessage.value = ''
  try {
    const result = await api.createRepairTasks(payload.issueIds, payload.runKey)
    batchMessage.value = `已创建 ${result.accepted} 个任务${result.rejected ? `，${result.rejected} 个未创建` : ''}`
    const failures = result.items.filter((item) => !item.accepted)
    await Promise.all([loadRepairCandidates(), loadData(false)])
    if (failures.length) {
      error.value = failures.map((item) => item.error).filter(Boolean).join('；')
    }
  } catch (cause) {
    error.value = friendlyError(cause)
  } finally {
    batchSubmitting.value = false
  }
}

async function deleteRepairTask(task: TaskView): Promise<void> {
  if (!window.confirm(`确定删除修复记录 ${shortId(task.id)} 吗？删除后任务统计会同步更新。`)) return
  try {
    await api.deleteTask(task.id)
    if (selectedTask.value?.id === task.id) selectedTask.value = null
    await Promise.all([loadRepairCandidates(), loadData(false)])
  } catch (cause) {
    error.value = friendlyError(cause)
  }
}

async function clearRepairTasks(): Promise<void> {
  if (!window.confirm('确定清空全部已完成的修复记录吗？排队中和执行中的任务会保留。')) return
  clearingTasks.value = true
  try {
    const result = await api.clearCompletedTasks()
    selectedTask.value = null
    taskPage.value = 0
    batchMessage.value = `已清空 ${result.deleted} 条修复记录${result.skippedActive ? `，保留 ${result.skippedActive} 条活跃任务` : ''}`
    await Promise.all([loadRepairCandidates(), loadData(false)])
  } catch (cause) {
    error.value = friendlyError(cause)
  } finally {
    clearingTasks.value = false
  }
}

async function loadTasks(): Promise<void> {
  if (!userId.value) return
  try {
    const page = await api.tasks(taskPage.value, 30, statusFilter.value || undefined)
    const enriched = await Promise.all(page.content.map(enrichTask))
    tasks.value = enriched
    totalPages.value = page.page.totalPages
    totalElements.value = page.page.totalElements
    if (selectedTask.value) {
      const updated = enriched.find((task) => task.id === selectedTask.value?.id)
      if (updated) selectedTask.value = updated
    }
  } catch (cause) {
    error.value = friendlyError(cause)
  }
}

async function enrichTask(task: TaskView): Promise<TaskView> {
  let issue = issueCache.get(task.issueId)
  if (!issue) {
    issue = await api.issue(task.issueId)
    issueCache.set(issue.id, issue)
  }
  let repository = repositoryCache.get(issue.repositoryId)
  if (!repository) {
    repository = await api.repository(issue.repositoryId)
    repositoryCache.set(repository.id, repository)
  }
  return { ...task, issue, repository }
}

async function openTask(value: TaskView | string): Promise<void> {
  if (typeof value !== 'string') {
    selectedTask.value = value
    return
  }
  const local = tasks.value.find((task) => task.id === value)
  if (local) {
    selectedTask.value = local
    return
  }
  try {
    selectedTask.value = await enrichTask(await api.task(value))
  } catch (cause) {
    error.value = friendlyError(cause)
  }
}

function applyUserId(): void {
  const value = draftUserId.value.trim()
  if (!/^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(value)) {
    error.value = '请输入有效的用户 UUID'
    return
  }
  saveUserId(value)
  userId.value = value
  showSettings.value = false
  void loadData(true)
}

function friendlyError(cause: unknown): string {
  const message = cause instanceof Error ? cause.message : '请求失败'
  if (message.includes('Failed to fetch')) return '无法连接后端，请检查后端服务和 Vite 代理配置。'
  return message.length > 240 ? '后端请求失败，请查看服务日志。' : message
}
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <a class="brand" href="#" aria-label="RepoAgent 监控首页">
        <span class="brand-mark"><Activity :size="20" /></span>
        <span><strong>RepoAgent</strong><small>任务监控</small></span>
      </a>
      <div class="topbar-actions">
        <span class="connection-state"><Wifi :size="15" />{{ error ? '连接异常' : '服务正常' }}</span>
        <span class="updated-at">更新于 {{ formatTime(lastUpdated) }}</span>
        <button class="icon-button" title="立即刷新" :disabled="refreshing" @click="loadData(false)"><RefreshCw :size="18" :class="{ spinning: refreshing }" /></button>
        <button class="icon-button" title="连接设置" @click="showSettings = true"><Settings :size="18" /></button>
      </div>
    </header>

    <main>
      <div class="page-heading">
        <div><p class="eyebrow">运行态总览</p><h1>修复任务监控</h1><p>查看队列、活跃子 Agent 和每个修复任务的实时阶段。</p></div>
        <div class="heading-status"><span class="pulse-dot" />每 5 秒自动刷新</div>
      </div>

      <div v-if="error" class="page-error"><TriangleAlert :size="18" /><span>{{ error }}</span><button @click="loadData(false)">重试</button></div>

      <section class="metric-grid">
        <MetricCard label="排队任务" :value="overview?.queuedTasks ?? '--'" hint="等待 Worker 领取" :icon="ListTodo" tone="warning" />
        <MetricCard label="执行中" :value="overview?.activeTasks ?? '--'" hint="分析、修复或验证" :icon="Activity" />
        <MetricCard label="今日完成" :value="overview?.completedToday ?? '--'" :hint="`${overview?.failedToday ?? 0} 个失败`" :icon="CheckCircle2" tone="success" />
        <MetricCard
          label="在线 Worker"
          :value="overview?.workerPresenceAvailable === false ? '--' : overview?.onlineWorkers ?? '--'"
          :hint="overview?.workerPresenceAvailable === false ? '在线状态暂不可用' : `${overview?.offlineWorkers ?? 0} 个离线`"
          :icon="Server"
        />
        <MetricCard label="平均执行" :value="formatDuration(overview?.averageExecutionMs)" hint="已记录任务的运行耗时" :icon="Clock3" />
        <MetricCard label="我的修复额度" :value="`${repairCapacity?.activeWorkflows ?? '--'} / ${repairCapacity?.maxActiveWorkflows ?? '--'}`" :hint="`今日剩余 ${repairCapacity?.remainingDailyQuota ?? '--'}，预计等待 ${repairCapacity?.estimatedStartWaitSeconds ?? '--'} 秒`" :icon="ListTodo" tone="warning" />
      </section>

      <section class="dashboard-grid">
        <WorkerPanel
          :workers="workers"
          :waiting-by-stage="overview?.byQueuedPipelineStage ?? {}"
          :presence-available="overview?.workerPresenceAvailable !== false"
          :loading="loading"
          @select="openTask"
        />
        <section class="panel distribution-panel">
          <header class="panel-header distribution-header">
            <div><h2>任务分布</h2><p>{{ distributionDescription }}</p></div>
            <span class="section-count">{{ distributionTotal }}</span>
          </header>
          <div class="distribution-tabs" role="tablist" aria-label="任务分布视图">
            <button :class="{ active: distributionMode === 'QUEUED' }" @click="distributionMode = 'QUEUED'">排队中</button>
            <button :class="{ active: distributionMode === 'ACTIVE' }" @click="distributionMode = 'ACTIVE'">修复中</button>
            <button :class="{ active: distributionMode === 'COMPLETED' }" @click="distributionMode = 'COMPLETED'">修复完成</button>
          </div>
          <div class="distribution-list">
            <div v-for="row in statusRows" :key="row.label" class="distribution-row">
              <div class="distribution-heading">
                <span><StatusBadge :status="row.status" :label="row.label" /><small v-if="row.detail">{{ row.detail }}</small></span>
                <strong>{{ row.count }}</strong>
              </div>
              <span class="distribution-track"><i :class="`bar-${row.status.toLowerCase()}`" :style="{ width: row.width }" /></span>
            </div>
          </div>
        </section>
      </section>

      <BatchRepairPanel
        :issues="repairCandidates"
        :loading="candidatesLoading"
        :submitting="batchSubmitting"
        :message="batchMessage"
        @refresh="loadRepairCandidates"
        @submit="submitRepairBatch"
      />

      <TaskTable
        :tasks="tasks"
        :loading="loading"
        :page="taskPage"
        :total-pages="totalPages"
        :total-elements="totalElements"
        :status="statusFilter"
        :clearing="clearingTasks"
        @select="openTask"
        @delete="deleteRepairTask"
        @clear="clearRepairTasks"
        @page="taskPage = $event"
        @update:status="statusFilter = $event"
      />
    </main>

    <TaskDrawer :task="selectedTask" @close="selectedTask = null" @changed="loadData(false)" />

    <Transition name="fade">
      <div v-if="showSettings" class="modal-layer">
        <button v-if="userId" class="modal-backdrop" aria-label="关闭设置" @click="showSettings = false" />
        <form class="settings-dialog" @submit.prevent="applyUserId">
          <span class="dialog-icon"><Settings :size="21" /></span>
          <h2>连接监控后端</h2>
          <p>开发阶段使用后端用户 UUID 作为访问身份。该值只保存在当前浏览器。</p>
          <label for="user-id">用户 UUID</label>
          <input id="user-id" v-model="draftUserId" autocomplete="off" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" />
          <div class="dialog-actions">
            <button v-if="userId" type="button" class="button secondary-button" @click="showSettings = false">取消</button>
            <button type="submit" class="button primary-button">连接后端</button>
          </div>
        </form>
      </div>
    </Transition>
  </div>
</template>
