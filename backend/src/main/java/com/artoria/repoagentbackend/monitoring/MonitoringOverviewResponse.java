package com.artoria.repoagentbackend.monitoring;

import com.artoria.repoagentbackend.repair.RepairTaskStatus;
import com.artoria.repoagentbackend.repair.RepairResultStatus;
import com.artoria.repoagentbackend.repair.RepairStageStatus;
import com.artoria.repoagentbackend.repair.RepairWorkflowStatus;

import java.time.Instant;
import java.util.Map;

public record MonitoringOverviewResponse(
        Instant generatedAt,
        long queuedTasks,
        long activeTasks,
        long completedToday,
        long failedToday,
        long onlineWorkers,
        long offlineWorkers,
        boolean workerPresenceAvailable,
        long averageQueueWaitMs,
        long averageExecutionMs,
        Map<RepairTaskStatus, Long> byStatus,
        Map<RepairTaskStatus, Long> byActiveStatus,
        Map<String, Long> byActiveStage,
        Map<String, Long> byQueuedPipelineStage,
        Map<RepairResultStatus, Long> byResultStatus,
        Map<RepairWorkflowStatus, Long> byWorkflowStatus,
        Map<RepairStageStatus, Long> byStageStatus
) {
}
