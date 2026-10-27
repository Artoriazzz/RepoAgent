package com.artoria.repoagentbackend.memory;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

import java.util.Map;
import java.util.UUID;

public record CreateMemoryObservationRequest(
        @NotNull UUID taskId,
        @NotBlank @Size(max = 191) String idempotencyKey,
        @NotNull MemoryOutcome outcome,
        @NotBlank String problemSignature,
        String patchSummary,
        String validationSummary,
        @NotNull Map<String, Object> evidence
) {
}
