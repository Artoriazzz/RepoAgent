package com.artoria.repoagentbackend.memory;

/** Qdrant 查询本身的结果，空命中与服务不可用必须分开处理。 */
public enum MemoryQdrantQueryStatus {
    DISABLED,
    SUCCESS,
    UNAVAILABLE
}
