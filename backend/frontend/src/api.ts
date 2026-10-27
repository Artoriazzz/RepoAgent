import type {
  Artifact,
  BatchRepairTaskResult,
  ClearRepairTasksResult,
  Issue,
  MonitoringOverview,
  MonitoringWorker,
  RepairAdmissionStatus,
  Page,
  RepairTask,
  Repository,
  TaskEvent,
  TaskStatus,
} from './types'

const USER_KEY = 'repoagent.monitor.userId'

export function getUserId(): string {
  return localStorage.getItem(USER_KEY) || import.meta.env.VITE_USER_ID || ''
}

export function saveUserId(userId: string): void {
  localStorage.setItem(USER_KEY, userId.trim())
}

function createRequestId(): string {
  const cryptoApi = globalThis.crypto
  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID()

  if (typeof cryptoApi?.getRandomValues === 'function') {
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16))
    return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
  }

  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('X-User-Id', getUserId())
  if (init.body) headers.set('Content-Type', 'application/json')
  const response = await fetch(path, { ...init, headers })
  if (!response.ok) {
    const message = await response.text()
    throw new Error(message || `请求失败 (${response.status})`)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  overview: () => request<MonitoringOverview>('/api/monitoring/overview'),
  workers: () => request<MonitoringWorker[]>('/api/monitoring/workers'),
  repairCapacity: () => request<RepairAdmissionStatus>('/api/repair-capacity'),
  tasks: (page = 0, size = 30, status?: TaskStatus) => {
    const query = new URLSearchParams({ page: String(page), size: String(size), sort: 'createdAt,desc' })
    if (status) query.set('status', status)
    return request<Page<RepairTask>>(`/api/repair-tasks?${query}`)
  },
  task: (id: string) => request<RepairTask>(`/api/repair-tasks/${id}`),
  repairableIssues: (page = 0, size = 50) => {
    const query = new URLSearchParams({
      page: String(page), size: String(size), state: 'OPEN', provider: 'SWE_BENCH',
      repairable: 'true', sort: 'createdAt,desc',
    })
    return request<Page<Issue>>(`/api/issues?${query}`)
  },
  createRepairTasks: (issueIds: string[], runKey?: string) => request<BatchRepairTaskResult>(
    '/api/repair-tasks/batch',
    {
      method: 'POST',
      headers: { 'Idempotency-Key': `dashboard-batch-${createRequestId()}` },
      body: JSON.stringify({ issueIds, runKey: runKey?.trim() || null }),
    },
  ),
  issue: (id: string) => request<Issue>(`/api/issues/${id}`),
  repository: (id: string) => request<Repository>(`/api/repositories/${id}`),
  events: (id: string) => request<Page<TaskEvent>>(`/api/repair-tasks/${id}/events?size=200`),
  artifacts: (id: string) => request<Artifact[]>(`/api/repair-tasks/${id}/artifacts`),
  cancel: (id: string) => request<RepairTask>(`/api/repair-tasks/${id}/cancel`, { method: 'POST' }),
  retry: (id: string) => request<RepairTask>(`/api/repair-tasks/${id}/retry`, {
    method: 'POST',
    headers: { 'Idempotency-Key': `dashboard-retry-${id}-${Date.now()}` },
  }),
  deleteTask: (id: string) => request<void>(`/api/repair-tasks/${id}`, { method: 'DELETE' }),
  clearCompletedTasks: () => request<ClearRepairTasksResult>('/api/repair-tasks/completed', {
    method: 'DELETE',
  }),
}

export async function downloadArtifact(taskId: string, artifact: Artifact): Promise<void> {
  const response = await fetch(`/api/repair-tasks/${taskId}/artifacts/${artifact.type}`, {
    headers: { 'X-User-Id': getUserId() },
  })
  if (!response.ok) throw new Error(`下载失败 (${response.status})`)
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = artifact.fileName
  anchor.click()
  URL.revokeObjectURL(url)
}

export async function streamTaskEvents(
  taskId: string,
  after: string | null,
  onEvent: (event: TaskEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const query = after ? `?after=${encodeURIComponent(after)}` : ''
  const response = await fetch(`/api/repair-tasks/${taskId}/events/stream${query}`, {
    headers: { Accept: 'text/event-stream', 'X-User-Id': getUserId() },
    signal,
  })
  if (!response.ok || !response.body) throw new Error(`实时连接失败 (${response.status})`)
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (!signal.aborted) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const name = block.split('\n').find((line) => line.startsWith('event:'))?.slice(6).trim()
      const data = block.split('\n').filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart()).join('\n')
      if (name === 'repair-task-event' && data) onEvent(JSON.parse(data) as TaskEvent)
      boundary = buffer.indexOf('\n\n')
    }
  }
}
