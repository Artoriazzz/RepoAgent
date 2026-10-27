package com.artoria.repoagentbackend.repair;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.PositiveOrZero;
import jakarta.validation.constraints.Size;

import java.util.UUID;

public record BenchmarkEvaluationUpdateRequest(
        @NotNull BenchmarkEvaluationStatus status,
        @PositiveOrZero Integer failToPassPassed,
        @PositiveOrZero Integer failToPassFailed,
        @PositiveOrZero Integer passToPassPassed,
        @PositiveOrZero Integer passToPassFailed,
        @Size(max = 20_000) String summary,
        @Size(max = 4_000) String harnessLogPath,
        @Size(max = 255) String workerId,
        UUID leaseToken,
        Long stageSequence,
        Long leaseEpoch
) {
    public BenchmarkEvaluationUpdateRequest(
            BenchmarkEvaluationStatus status, Integer failToPassPassed, Integer failToPassFailed,
            Integer passToPassPassed, Integer passToPassFailed, String summary, String harnessLogPath,
            String workerId, UUID leaseToken
    ) {
        this(status, failToPassPassed, failToPassFailed, passToPassPassed, passToPassFailed,
                summary, harnessLogPath, workerId, leaseToken, null, null);
    }
    public long effectiveStageSequence(long current) { return stageSequence == null ? current : stageSequence; }
    public long effectiveLeaseEpoch(long current) { return leaseEpoch == null ? current : leaseEpoch; }
}
