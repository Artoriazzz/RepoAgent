package com.artoria.repoagentbackend.memory;

import java.util.List;

/**
 * Qdrant 成功返回空列表表示“没有相似记忆”，不是缓存缺失。
 * UNAVAILABLE 时调用方会让 Agent 在没有记忆的情况下继续，而不会扫描 MySQL 候选池。
 */
public record MemoryQdrantQueryResult(
        MemoryQdrantQueryStatus status,
        List<MemoryQdrantSearchHit> hits,
        String detail
) {
    public MemoryQdrantQueryResult {
        hits = hits == null ? List.of() : List.copyOf(hits);
        detail = detail == null ? "" : detail;
    }

    public static MemoryQdrantQueryResult disabled() {
        return new MemoryQdrantQueryResult(MemoryQdrantQueryStatus.DISABLED, List.of(), "Qdrant is disabled");
    }

    public static MemoryQdrantQueryResult success(List<MemoryQdrantSearchHit> hits) {
        return new MemoryQdrantQueryResult(MemoryQdrantQueryStatus.SUCCESS, hits, "");
    }

    public static MemoryQdrantQueryResult unavailable(String detail) {
        return new MemoryQdrantQueryResult(MemoryQdrantQueryStatus.UNAVAILABLE, List.of(), detail);
    }
}
