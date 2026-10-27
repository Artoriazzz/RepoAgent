package com.artoria.repoagentbackend.memory;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;

import java.util.UUID;

public record MemoryMergeBatchCompletionRequest(
        @NotBlank String workerId,
        @NotNull UUID leaseToken,
        @NotNull MemoryMergeAction action,
        @Valid MemoryContent content,
        String changeReason
) {
}
