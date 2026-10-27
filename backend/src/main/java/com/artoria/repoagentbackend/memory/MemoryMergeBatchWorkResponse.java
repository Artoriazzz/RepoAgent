package com.artoria.repoagentbackend.memory;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

public record MemoryMergeBatchWorkResponse(
        UUID batchId,
        UUID leaseToken,
        Instant leaseExpiresAt,
        MemoryProposalOperation operation,
        UUID memoryId,
        Long baseVersion,
        MemoryScopeType scopeType,
        UUID repositoryId,
        MemoryResponse currentMemory,
        List<MemoryMergeProposalInput> proposals
) {
}
