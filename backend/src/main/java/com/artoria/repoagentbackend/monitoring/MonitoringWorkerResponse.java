package com.artoria.repoagentbackend.monitoring;

import com.artoria.repoagentbackend.repair.RepairTaskStatus;

import java.time.Instant;
import java.util.UUID;

public record MonitoringWorkerResponse(
        String workerId,
        String workerRole,
        String availability,
        String reportedStatus,
        Instant lastHeartbeatAt,
        UUID activeTaskId,
        RepairTaskStatus taskStatus,
        String currentStage,
        String currentMessage,
        String repositoryName,
        String issueTitle,
        Instant taskStartedAt
) {
}
