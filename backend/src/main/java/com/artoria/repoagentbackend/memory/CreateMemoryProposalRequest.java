package com.artoria.repoagentbackend.memory;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

import java.util.UUID;

public record CreateMemoryProposalRequest(
        @NotNull UUID observationId,
        @NotBlank @Size(max = 191) String idempotencyKey,
        @NotNull MemoryProposalOperation operation,
        UUID memoryId,
        Long baseVersion,
        MemoryScopeType scopeType,
        @Valid MemoryContent content,
        @NotBlank String changeReason
) {
}
