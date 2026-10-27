package com.artoria.repoagentbackend.memory;

import java.time.Instant;
import java.util.UUID;

public record MemoryResponse(
        UUID id,
        MemoryScopeType scopeType,
        String scopeKey,
        UUID repositoryId,
        long revision,
        MemoryStatus status,
        double qualityScore,
        long successfulUses,
        long failedUses,
        MemoryContent content,
        String embeddingModel,
        String embeddingProfile,
        Integer embeddingDimensions,
        Long embeddingRevision,
        Instant updatedAt
) {
    static MemoryResponse from(SemanticMemory memory) {
        return new MemoryResponse(
                memory.getId(), memory.getScopeType(), memory.getScopeKey(), memory.getRepositoryId(),
                memory.getVersion(), memory.getStatus(), memory.getQualityScore(),
                memory.getSuccessfulUses(), memory.getFailedUses(), memory.content(),
                memory.getEmbeddingModel(), memory.getEmbeddingProfile(), memory.getEmbeddingDimensions(),
                memory.getEmbeddingRevision(), memory.getUpdatedAt()
        );
    }

    static MemoryResponse fromCached(
            MemoryScopeEntry entry,
            MemoryContent content,
            MemoryVectorValue vector,
            String embeddingProfile
    ) {
        return new MemoryResponse(
                entry.memoryId(), entry.scopeType(), entry.scopeKey(), entry.repositoryId(),
                entry.revision(), entry.status(), entry.qualityScore(), entry.successfulUses(),
                entry.failedUses(), content, vector.model(), embeddingProfile, vector.dimensions(),
                entry.revision(), entry.updatedAt()
        );
    }
}
