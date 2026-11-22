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
        long leaseEpoch,
        RepairAgentContextStore.Snapshot ownContext,
        RepairAgentContextStore.Snapshot incomingContext
) {
    static RepairTaskControlResponse from(RepairTask task) {
        return new RepairTaskControlResponse(
                task.getId(), task.getStatus(), task.isCancelRequested(), task.getWorkerId(),
                task.getLeaseToken(), task.getLeaseExpiresAt(), task.getWorkflowStatus(),
                task.getCurrentPipelineStage(), task.getStageStatus(), task.getStageSequence(), task.getLeaseEpoch(),
                null, null
        );
    }

    RepairTaskControlResponse withContexts(RepairAgentContextStore.Snapshot own,
                                           RepairAgentContextStore.Snapshot incoming) {
        return new RepairTaskControlResponse(taskId, status, cancelRequested, workerId, leaseToken, leaseExpiresAt,
                workflowStatus, currentPipelineStage, stageStatus, stageSequence, leaseEpoch, own, incoming);
    }
}
