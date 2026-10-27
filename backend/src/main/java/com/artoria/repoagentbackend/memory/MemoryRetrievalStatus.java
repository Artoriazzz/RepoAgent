package com.artoria.repoagentbackend.memory;

/** 供 Worker 区分“无可用经验”和“记忆检索服务暂时不可用”。 */
public enum MemoryRetrievalStatus {
    QDRANT_HIT,
    QDRANT_EMPTY,
    MEMORY_UNAVAILABLE,
    REDIS_VECTOR_HIT,
    REDIS_VECTOR_EMPTY,
    KEYWORD_HIT,
    KEYWORD_EMPTY
}
