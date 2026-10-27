package com.artoria.repoagentbackend.repair;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

import java.util.UUID;

public record RepairTaskLeaseRequest(
        @NotBlank @Size(max = 255) String workerId,
        @NotNull UUID leaseToken,
        Long stageSequence,
        Long leaseEpoch,
        @Min(30) @Max(600) Integer leaseSeconds
) {
    public int effectiveLeaseSeconds() {
        return leaseSeconds == null ? 90 : leaseSeconds;
    }
    public long effectiveStageSequence(long current) { return stageSequence == null ? current : stageSequence; }
    public long effectiveLeaseEpoch(long current) { return leaseEpoch == null ? current : leaseEpoch; }
}
