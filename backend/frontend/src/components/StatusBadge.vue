<script setup lang="ts">
import type { TaskStatus } from '../types'

defineProps<{ status: TaskStatus | string | null; label?: string }>()

const labels: Record<string, string> = {
  CREATED: '已创建', QUEUED: '排队中', ANALYZING: '分析中', FIXING: '修复中',
  VALIDATING: '验证中', PATCH_READY: '补丁就绪', PR_READY: 'PR 就绪',
  FAILED: '失败', CANCELLED: '已取消', ONLINE: '在线', OFFLINE: '离线',
  IDLE: '空闲', BUSY: '忙碌', RESOLVED: '已解决', UNRESOLVED: '未解决',
  HARNESS_ERROR: '评测错误', NOT_RUN: '未评测', RUNNING: '评测中',
  COORDINATOR: 'Coordinator 定位', IMPLEMENT: 'Implement 修改',
  PUBLIC_VALIDATION: '公开验证', FAILURE_REVIEW: 'FailureReview 分析',
  CORRECTIVE_IMPLEMENT: '纠正修复', FINAL_EVALUATION: '最终评测',
  RETRYING: '重试中', EXPERIENCE_MEMORY: '经验记忆中',
  SUCCEEDED: '修复成功', ENVIRONMENT_ERROR: '环境错误', PENDING: '尚未完成',
  COMPLETED: '执行结束',
}
</script>

<template>
  <span v-if="status" class="status-badge" :class="`status-${status.toLowerCase()}`">
    <span class="status-dot" />{{ label || labels[status] || status }}
  </span>
  <span v-else class="muted">--</span>
</template>
