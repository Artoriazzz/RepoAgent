package com.artoria.repoagentbackend.repair;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record RepairTaskClaimRequest(
        @NotBlank @Size(max = 255) String workerId,
        @Min(30) @Max(600) Integer leaseSeconds,
        RepairPipelineStage stage,
        Long stageSequence
) {
    public int effectiveLeaseSeconds() {
        return leaseSeconds == null ? 90 : leaseSeconds;
    }

    public RepairPipelineStage effectiveStage() {
        return stage == null ? RepairPipelineStage.COORDINATOR : stage;
    }

    public long effectiveStageSequence(long current) { return stageSequence == null ? current : stageSequence; }
}
