package com.artoria.repoagentbackend.repair;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.PositiveOrZero;
import jakarta.validation.constraints.Size;

import java.util.UUID;

/** Finalizes a pipeline only after the experience-memory stage has completed. */
public record RepairTaskPipelineCompletionRequest(
        @NotBlank @Size(max = 255) String workerId,
        @NotNull UUID leaseToken,
        Long stageSequence,
        Long leaseEpoch,
        @Size(max = 20_000) String patchSummary,
        @Size(max = 4_000) String patchArtifactPath,
        @Size(max = 20_000) String validationSummary,
        @NotNull BenchmarkEvaluationStatus evaluationStatus,
        @PositiveOrZero Integer failToPassPassed,
        @PositiveOrZero Integer failToPassFailed,
        @PositiveOrZero Integer passToPassPassed,
        @PositiveOrZero Integer passToPassFailed,
        @Size(max = 20_000) String evaluationSummary,
        @Size(max = 4_000) String harnessLogPath,
        @Size(max = 255) String modelName,
        @PositiveOrZero Long durationMs,
        @PositiveOrZero Long promptTokens,
        @PositiveOrZero Long completionTokens,
        @PositiveOrZero Long totalTokens
) {
    public RepairTaskPipelineCompletionRequest(
            String workerId, UUID leaseToken, String patchSummary, String patchArtifactPath,
            String validationSummary, BenchmarkEvaluationStatus evaluationStatus,
            Integer failToPassPassed, Integer failToPassFailed, Integer passToPassPassed,
            Integer passToPassFailed, String evaluationSummary, String harnessLogPath,
            String modelName, Long durationMs, Long promptTokens, Long completionTokens, Long totalTokens
    ) {
        this(workerId, leaseToken, null, null, patchSummary, patchArtifactPath, validationSummary,
                evaluationStatus, failToPassPassed, failToPassFailed, passToPassPassed, passToPassFailed,
                evaluationSummary, harnessLogPath, modelName, durationMs, promptTokens, completionTokens, totalTokens);
    }
    public long effectiveStageSequence(long current) { return stageSequence == null ? current : stageSequence; }
    public long effectiveLeaseEpoch(long current) { return leaseEpoch == null ? current : leaseEpoch; }
}
