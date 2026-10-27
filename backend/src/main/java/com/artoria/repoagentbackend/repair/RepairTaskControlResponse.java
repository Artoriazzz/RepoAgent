package com.artoria.repoagentbackend.repair;

import java.time.Instant;
import java.util.UUID;

public record RepairTaskControlResponse(
        UUID taskId,
        RepairTaskStatus status,
        boolean cancelRequested,
        String workerId,
        UUID leaseToken,
        Instant leaseExpiresAt,
        RepairWorkflowStatus workflowStatus,
        RepairPipelineStage currentPipelineStage,
        RepairStageStatus stageStatus,
        long stageSequence,
        long leaseEpoch
) {
    static RepairTaskControlResponse from(RepairTask task) {
        return new RepairTaskControlResponse(
                task.getId(), task.getStatus(), task.isCancelRequested(), task.getWorkerId(),
                task.getLeaseToken(), task.getLeaseExpiresAt(), task.getWorkflowStatus(),
                task.getCurrentPipelineStage(), task.getStageStatus(), task.getStageSequence(), task.getLeaseEpoch()
        );
    }
}
