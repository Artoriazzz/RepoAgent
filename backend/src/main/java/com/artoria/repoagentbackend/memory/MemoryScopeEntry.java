package com.artoria.repoagentbackend.memory;

import java.time.Instant;
import java.util.UUID;

/**
 * 作用域缓存中的轻量成员项。正文和向量各自按版本单独缓存，避免索引缓存携带大字段。
 */
public record MemoryScopeEntry(
        UUID memoryId,
        long revision,
        MemoryScopeType scopeType,
        String scopeKey,
        UUID repositoryId,
        MemoryStatus status,
        double qualityScore,
        long successfulUses,
        long failedUses,
        Instant updatedAt
) {
    static MemoryScopeEntry from(SemanticMemory memory) {
        return new MemoryScopeEntry(
                memory.getId(), memory.getVersion(), memory.getScopeType(), memory.getScopeKey(),
                memory.getRepositoryId(), memory.getStatus(), memory.getQualityScore(),
                memory.getSuccessfulUses(), memory.getFailedUses(), memory.getUpdatedAt()
        );
    }
}
