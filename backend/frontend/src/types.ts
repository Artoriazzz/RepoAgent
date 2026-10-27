export type TaskStatus =
  | 'CREATED'
  | 'QUEUED'
  | 'ANALYZING'
  | 'FIXING'
  | 'VALIDATING'
  | 'PATCH_READY'
  | 'PR_READY'
  | 'FAILED'
  | 'CANCELLED'

export interface Page<T> {
  content: T[]
  page: { size: number; number: number; totalElements: number; totalPages: number }
}

export interface MonitoringOverview {
  generatedAt: string
  queuedTasks: number
  activeTasks: number
  completedToday: number
  failedToday: number
  onlineWorkers: number
  offlineWorkers: number
  workerPresenceAvailable: boolean
  averageQueueWaitMs: number
  averageExecutionMs: number
  byStatus: Partial<Record<TaskStatus, number>>
  byActiveStatus: Partial<Record<TaskStatus, number>>
  byActiveStage: Partial<Record<string, number>>
  byQueuedPipelineStage: Partial<Record<string, number>>
  byResultStatus: Partial<Record<RepairTask['resultStatus'], number>>
  byWorkflowStatus: Partial<Record<WorkflowStatus, number>>
  byStageStatus: Partial<Record<StageStatus, number>>
}

export type WorkflowStatus = 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
export type StageStatus = 'WAITING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED'

export interface RepairAdmissionStatus {
  activeWorkflows: number
  maxActiveWorkflows: number
  remainingActiveSlots: number
  acceptedToday: number
  dailyTaskLimit: number
  remainingDailyQuota: number
  globalActiveWorkflows: number
  globalMaxInflight: number
  waitingStages: number
  estimatedStartWaitSeconds: number
  dailyQuotaResetsAt: string
}

export interface MonitoringWorker {
  workerId: string
  workerRole: string | null
  availability: 'ONLINE' | 'OFFLINE' | 'UNAVAILABLE'
  reportedStatus: string
  lastHeartbeatAt: string
  activeTaskId: string | null
  taskStatus: TaskStatus | null
  currentStage: string | null
  currentMessage: string | null
  repositoryName: string | null
  issueTitle: string | null
  taskStartedAt: string | null
}

export interface RepairTask {
  id: string
  issueId: string
  baseSha: string
  branchName: string
  status: TaskStatus
  patchSummary: string | null
  patchArtifactPath: string | null
  validationSummary: string | null
  prUrl: string | null
  evaluationStatus: string
  failToPassPassed: number | null
  failToPassFailed: number | null
  passToPassPassed: number | null
  passToPassFailed: number | null
  evaluationSummary: string | null
  parentTaskId: string | null
  attemptNumber: number
  modelName: string | null
  runKey: string | null
  errorCode: string | null
  errorSummary: string | null
  retryable: boolean | null
  cancelRequestedAt: string | null
  workerId: string | null
  leaseExpiresAt: string | null
  lastHeartbeatAt: string | null
  durationMs: number | null
  promptTokens: number | null
  completionTokens: number | null
  totalTokens: number | null
  currentStage: string | null
  currentMessage: string | null
  pipelineStage: string
  workflowStatus: WorkflowStatus
  currentPipelineStage: string
  stageStatus: StageStatus
  stageSequence: number
  leaseEpoch: number
  admittedAt: string | null
  correctionAttempt: number
  maxCorrectionAttempts: number
  resultStatus: 'PENDING' | 'SUCCEEDED' | 'FAILED' | 'ENVIRONMENT_ERROR' | 'CANCELLED'
  version: number
  createdAt: string
  completedAt: string | null
}

export interface Issue {
  id: string
  repositoryId: string
  githubIssueNumber: number | null
  externalId: string | null
  benchmarkDatasetSplit: string | null
  baseSha: string | null
  title: string
  body: string
  state: string
}

export interface IssueView extends Issue {
  repository?: Repository
}

export interface BatchRepairTaskItem {
  issueId: string
  accepted: boolean
  task: RepairTask | null
  error: string | null
}

export interface BatchRepairTaskResult {
  requested: number
  accepted: number
  rejected: number
  items: BatchRepairTaskItem[]
}

export interface ClearRepairTasksResult {
  deleted: number
  skippedActive: number
}

export interface Repository {
  id: string
  provider: string
  externalId: string | null
  ownerLogin: string
  name: string
  defaultBranch: string
}

export interface TaskEvent {
  id: string
  eventType: string
  stage: string | null
  message: string | null
  metadataJson: string | null
  createdAt: string
}

export interface Artifact {
  type: string
  fileName: string
  size: number
  available: boolean
}

export interface TaskView extends RepairTask {
  issue?: Issue
  repository?: Repository
}
