package com.artoria.repoagentbackend.memory;

/** Qdrant 返回的记忆 payload 与向量相似度。 */
public record MemoryQdrantSearchHit(
        MemoryResponse memory,
        double similarity,
        double score
) {
}
