package com.artoria.repoagentbackend.repair;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.PositiveOrZero;
import jakarta.validation.constraints.Size;

import java.util.UUID;

public record RepairTaskWorkerUpdateRequest(
        @NotNull RepairTaskStatus status,
        @Size(max = 20_000) String patchSummary,
        @Size(max = 4_000) String patchArtifactPath,
        @Size(max = 20_000) String validationSummary,
        @Size(max = 2_000) String prUrl,
        @Size(max = 255) String workerId,
        UUID leaseToken,
        Long stageSequence,
        Long leaseEpoch,
        @Size(max = 64) String errorCode,
        @Size(max = 20_000) String errorSummary,
        Boolean retryable,
        @Size(max = 255) String modelName,
        @PositiveOrZero Long durationMs,
        @PositiveOrZero Long promptTokens,
        @PositiveOrZero Long completionTokens,
        @PositiveOrZero Long totalTokens
) {
    public long effectiveStageSequence(long current) { return stageSequence == null ? current : stageSequence; }
    public long effectiveLeaseEpoch(long current) { return leaseEpoch == null ? current : leaseEpoch; }
}
