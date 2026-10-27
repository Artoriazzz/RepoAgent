<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { AlertTriangle, Ban, Download, GitBranch, RefreshCw, RotateCcw, TestTube2, Trash2, X } from '@lucide/vue'
import { api, downloadArtifact, streamTaskEvents } from '../api'
import { formatDuration, formatNumber, formatTime, shortId } from '../format'
import type { Artifact, TaskEvent, TaskStatus, TaskView } from '../types'
import StatusBadge from './StatusBadge.vue'

const props = defineProps<{ task: TaskView | null }>()
const emit = defineEmits<{ close: []; changed: [] }>()

const events = ref<TaskEvent[]>([])
const artifacts = ref<Artifact[]>([])
const loading = ref(false)
const actionLoading = ref(false)
const streamState = ref<'connecting' | 'live' | 'retrying'>('connecting')
const error = ref('')
let streamController: AbortController | null = null

const terminal = computed(() => ['PATCH_READY', 'PR_READY', 'FAILED', 'CANCELLED'].includes(props.task?.status || ''))
const cancellable = computed(() => !!props.task && !terminal.value && !props.task.cancelRequestedAt)
const retryableTask = computed(() => (
  !!props.task && terminal.value && props.task.attemptNumber < 2
  && props.task.resultStatus !== 'SUCCEEDED'
))
const deletable = computed(() => !!props.task && props.task.resultStatus !== 'PENDING')

const stages: Array<{ status: TaskStatus; label: string }> = [
  { status: 'QUEUED', label: '排队' }, { status: 'ANALYZING', label: '分析' },
  { status: 'FIXING', label: '修改代码' }, { status: 'VALIDATING', label: '验证' },
  { status: 'PATCH_READY', label: '补丁就绪' },
]
const stageIndex = computed(() => stages.findIndex((stage) => stage.status === props.task?.status))

watch(() => props.task?.id, async (taskId) => {
  streamController?.abort()
  events.value = []
  artifacts.value = []
  error.value = ''
  if (!taskId) return
  loading.value = true
  try {
    const [eventPage, artifactList] = await Promise.all([api.events(taskId), api.artifacts(taskId)])
    events.value = eventPage.content
    artifacts.value = artifactList.filter((artifact) => artifact.available)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '任务详情加载失败'
  } finally {
    loading.value = false
  }
  connectStream(taskId)
}, { immediate: true })

onBeforeUnmount(() => streamController?.abort())

async function connectStream(taskId: string): Promise<void> {
  streamController?.abort()
  const controller = new AbortController()
  streamController = controller
  let retryDelay = 1_000
  while (!controller.signal.aborted && props.task?.id === taskId) {
    streamState.value = retryDelay === 1_000 ? 'connecting' : 'retrying'
    try {
      const after = events.value.length ? events.value[events.value.length - 1].createdAt : null
      streamState.value = 'live'
      await streamTaskEvents(taskId, after, appendEvent, controller.signal)
      retryDelay = 1_000
      if (!controller.signal.aborted) {
        await new Promise((resolve) => window.setTimeout(resolve, retryDelay))
      }
    } catch (cause) {
      if (controller.signal.aborted) return
      streamState.value = 'retrying'
      await new Promise((resolve) => window.setTimeout(resolve, retryDelay))
      retryDelay = Math.min(retryDelay * 2, 10_000)
    }
  }
}

function appendEvent(event: TaskEvent): void {
  if (events.value.some((existing) => existing.id === event.id)) return
  events.value.push(event)
  emit('changed')
}

async function cancelTask(): Promise<void> {
  if (!props.task || !window.confirm('确定取消这个修复任务吗？')) return
  actionLoading.value = true
  try {
    await api.cancel(props.task.id)
    emit('changed')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '取消失败'
  } finally {
    actionLoading.value = false
  }
}

async function retryTask(): Promise<void> {
  if (!props.task) return
  actionLoading.value = true
  try {
    await api.retry(props.task.id)
    emit('changed')
    emit('close')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '重试失败'
  } finally {
    actionLoading.value = false
  }
}

async function deleteTask(): Promise<void> {
  if (!props.task || !window.confirm('确定删除这条修复记录吗？删除后任务统计会同步更新。')) return
  actionLoading.value = true
  try {
    await api.deleteTask(props.task.id)
    emit('close')
    emit('changed')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '删除失败'
  } finally {
    actionLoading.value = false
  }
}

async function download(artifact: Artifact): Promise<void> {
  if (!props.task) return
  try {
    await downloadArtifact(props.task.id, artifact)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '下载失败'
  }
}
</script>

<template>
  <Transition name="drawer">
    <div v-if="task" class="drawer-layer">
      <button class="drawer-backdrop" aria-label="关闭任务详情" @click="$emit('close')" />
      <aside class="task-drawer" aria-label="任务详情">
        <header class="drawer-header">
          <div>
            <p class="eyebrow">修复任务 {{ shortId(task.id) }}</p>
            <h2>{{ task.issue?.title || '任务详情' }}</h2>
            <span>{{ task.repository ? `${task.repository.ownerLogin}/${task.repository.name}` : task.branchName }}</span>
          </div>
          <button class="icon-button" title="关闭" @click="$emit('close')"><X :size="20" /></button>
        </header>

        <div class="drawer-statusbar">
          <StatusBadge :status="task.resultStatus === 'PENDING' ? (task.currentStage || task.status) : 'COMPLETED'" />
          <StatusBadge :status="task.resultStatus" />
          <span class="live-indicator" :class="streamState"><i />{{ streamState === 'live' ? '实时更新' : streamState === 'retrying' ? '正在重连' : '正在连接' }}</span>
        </div>

        <div v-if="error" class="inline-error"><AlertTriangle :size="16" />{{ error }}</div>

        <div class="drawer-content">
          <section class="detail-section">
            <h3>执行阶段</h3>
            <div class="stage-track">
              <div v-for="(stage, index) in stages" :key="stage.status" class="stage-item" :class="{ done: index < stageIndex || ['PATCH_READY', 'PR_READY'].includes(task.status), current: stage.status === task.status }">
                <span>{{ index + 1 }}</span><b>{{ stage.label }}</b>
              </div>
            </div>
          </section>

          <section class="detail-section detail-grid">
            <div><span>Worker</span><strong class="mono">{{ task.workerId || '尚未分配' }}</strong></div>
            <div><span>模型</span><strong>{{ task.modelName || '--' }}</strong></div>
            <div><span>执行耗时</span><strong>{{ formatDuration(task.durationMs) }}</strong></div>
            <div><span>Token</span><strong>{{ formatNumber(task.totalTokens) }}</strong></div>
            <div><span>尝试次数</span><strong>{{ task.attemptNumber }}</strong></div>
            <div><span>纠正机会</span><strong>{{ task.correctionAttempt }}/{{ task.maxCorrectionAttempts }}</strong></div>
            <div><span>创建时间</span><strong>{{ formatTime(task.createdAt) }}</strong></div>
          </section>

          <section class="detail-section">
            <h3><GitBranch :size="16" />执行信息</h3>
            <dl class="definition-list">
              <div><dt>分支</dt><dd class="mono">{{ task.branchName }}</dd></div>
              <div><dt>基线</dt><dd class="mono">{{ task.baseSha }}</dd></div>
              <div v-if="task.runKey"><dt>运行批次</dt><dd>{{ task.runKey }}</dd></div>
            </dl>
          </section>

          <section v-if="task.evaluationStatus !== 'NOT_RUN'" class="detail-section">
            <h3><TestTube2 :size="16" />验证结果</h3>
            <div class="evaluation-line">
              <StatusBadge :status="task.evaluationStatus" />
              <span>F2P {{ task.failToPassPassed ?? 0 }}/{{ (task.failToPassPassed ?? 0) + (task.failToPassFailed ?? 0) }}</span>
              <span>P2P {{ task.passToPassPassed ?? 0 }}/{{ (task.passToPassPassed ?? 0) + (task.passToPassFailed ?? 0) }}</span>
            </div>
            <p v-if="task.evaluationSummary" class="summary-text">{{ task.evaluationSummary }}</p>
          </section>

          <section v-if="task.errorCode" class="detail-section error-section">
            <h3><AlertTriangle :size="16" />{{ task.errorCode }}</h3>
            <p>{{ task.errorSummary }}</p>
          </section>

          <section class="detail-section">
            <h3>事件时间线</h3>
            <div v-if="loading" class="timeline-loading">正在读取事件...</div>
            <ol v-else class="timeline">
              <li v-for="event in [...events].reverse()" :key="event.id">
                <i />
                <div><strong>{{ event.stage || event.eventType }}</strong><time>{{ formatTime(event.createdAt) }}</time><p>{{ event.message || event.eventType }}</p></div>
              </li>
            </ol>
          </section>

          <section v-if="artifacts.length" class="detail-section">
            <h3>运行产物</h3>
            <button v-for="artifact in artifacts" :key="artifact.type" class="artifact-row" @click="download(artifact)">
              <span><Download :size="16" /><b>{{ artifact.fileName }}</b></span><small>{{ formatNumber(artifact.size) }} B</small>
            </button>
          </section>
        </div>

        <footer class="drawer-actions">
          <button v-if="deletable" class="button danger-button" :disabled="actionLoading" @click="deleteTask"><Trash2 :size="16" />删除记录</button>
          <button v-if="cancellable" class="button danger-button" :disabled="actionLoading" @click="cancelTask"><Ban :size="16" />取消任务</button>
          <button v-if="retryableTask" class="button primary-button" :disabled="actionLoading" @click="retryTask"><RotateCcw :size="16" />错误分析后重试（仅一次）</button>
          <button class="button secondary-button" :disabled="loading" @click="$emit('changed')"><RefreshCw :size="16" />刷新</button>
        </footer>
      </aside>
    </div>
  </Transition>
</template>
