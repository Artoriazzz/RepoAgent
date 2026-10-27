package com.artoria.repoagentbackend.repair;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

import java.util.UUID;

public record RepairTaskProgressRequest(
        @NotBlank @Size(max = 64) String stage,
        @NotBlank @Size(max = 20_000) String message,
        @Size(max = 100_000) String metadataJson,
        @NotBlank @Size(max = 255) String workerId,
        UUID leaseToken,
        Long stageSequence,
        Long leaseEpoch
) {
    public long effectiveStageSequence(long current) { return stageSequence == null ? current : stageSequence; }
    public long effectiveLeaseEpoch(long current) { return leaseEpoch == null ? current : leaseEpoch; }
}
