package com.artoria.repoagentbackend.repair;

import java.time.Instant;
import java.util.UUID;

public record RepairTaskProgressResponse(
        UUID taskId,
        RepairTaskStatus status,
        boolean cancelRequested,
        String workerId,
        Instant leaseExpiresAt,
        RepairWorkflowStatus workflowStatus,
        RepairPipelineStage currentPipelineStage,
        RepairStageStatus stageStatus,
        long stageSequence,
        RepairTaskEventResponse latestEvent
) {
}
