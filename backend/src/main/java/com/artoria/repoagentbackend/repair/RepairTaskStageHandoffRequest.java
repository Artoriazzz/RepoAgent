package com.artoria.repoagentbackend.repair;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

import java.util.UUID;

/** Request used by a stage Worker to atomically release its lease and enqueue the next stage. */
public record RepairTaskStageHandoffRequest(
        @NotNull RepairPipelineStage currentStage,
        @NotNull RepairPipelineStage nextStage,
        @NotBlank @Size(max = 255) String workerId,
        @NotNull UUID leaseToken,
        Long stageSequence,
        Long leaseEpoch,
        @Size(max = 20_000) String message
) {
    public long effectiveStageSequence(long current) { return stageSequence == null ? current : stageSequence; }
    public long effectiveLeaseEpoch(long current) { return leaseEpoch == null ? current : leaseEpoch; }
}
