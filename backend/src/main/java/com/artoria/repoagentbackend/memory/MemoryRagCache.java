package com.artoria.repoagentbackend.memory;

import com.artoria.repoagentbackend.common.RedisJsonCache;
import com.fasterxml.jackson.core.type.TypeReference;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Supplier;

/**
 * 记忆 RAG 的可丢失 Redis 加速层。
 *
 * <p>作用域索引只保存记忆定位和排序元数据，向量与正文按 memoryId + revision 独立缓存。
 * 任何缓存故障都由调用方回源 MySQL，不影响召回正确性。</p>
 */
@Component
public class MemoryRagCache {
    private static final TypeReference<List<MemoryScopeEntry>> SCOPE_ENTRIES = new TypeReference<>() { };

    private final RedisJsonCache cache;
    private final boolean enabled;
    private final String embeddingProfile;
    private final Duration indexTtl;
    private final Duration vectorTtl;
    private final Duration contentTtl;
    private final Duration rebuildLockTtl;
    private final Duration rebuildWait;

    public MemoryRagCache(
            RedisJsonCache cache,
            @Value("${app.memory.rag-cache.enabled:true}") boolean enabled,
            @Value("${app.memory.embedding.profile:memory-retrieval-v1}") String embeddingProfile,
            @Value("${app.memory.rag-cache.index-ttl-seconds:20}") long indexTtlSeconds,
            @Value("${app.memory.rag-cache.vector-ttl-seconds:86400}") long vectorTtlSeconds,
            @Value("${app.memory.rag-cache.content-ttl-seconds:86400}") long contentTtlSeconds,
            @Value("${app.memory.rag-cache.rebuild-lock-ttl-millis:3000}") long rebuildLockTtlMillis,
            @Value("${app.memory.rag-cache.rebuild-wait-millis:120}") long rebuildWaitMillis
    ) {
        this.cache = cache;
        this.enabled = enabled;
        this.embeddingProfile = normalizedProfile(embeddingProfile);
        this.indexTtl = Duration.ofSeconds(Math.max(1, indexTtlSeconds));
        this.vectorTtl = Duration.ofSeconds(Math.max(1, vectorTtlSeconds));
        this.contentTtl = Duration.ofSeconds(Math.max(1, contentTtlSeconds));
        this.rebuildLockTtl = Duration.ofMillis(Math.max(100, rebuildLockTtlMillis));
        this.rebuildWait = Duration.ofMillis(Math.max(0, rebuildWaitMillis));
    }

    public String embeddingProfile() {
        return embeddingProfile;
    }

    public Optional<List<MemoryScopeEntry>> findScopeEntries(String scopeKey) {
        if (!enabled) return Optional.empty();
        return cache.get(scopeIndexKey(scopeKey), SCOPE_ENTRIES).map(List::copyOf);
    }

    /**
     * Redis 作为向量检索的备用路径时，热点作用域索引失效不能让所有请求同时读取 MySQL。
     * 只有锁持有者回填；未抢到锁的请求短暂等候，仍未命中便暂时不用该作用域记忆。
     */
    public List<MemoryScopeEntry> findOrLoadScopeEntries(
            String scopeKey, Supplier<List<MemoryScopeEntry>> databaseLoader
    ) {
        Optional<List<MemoryScopeEntry>> cached = findScopeEntries(scopeKey);
        if (cached.isPresent()) return cached.get();
        if (!enabled) return databaseLoader.get();

        String lockKey = "memory:rebuild:" + scopeIndexKey(scopeKey);
        String token = UUID.randomUUID().toString();
        if (cache.tryAcquireLock(lockKey, token, rebuildLockTtl)) {
            try {
                return findScopeEntries(scopeKey).orElseGet(() -> {
                    List<MemoryScopeEntry> loaded = List.copyOf(databaseLoader.get());
                    cacheScopeEntries(scopeKey, loaded);
                    return loaded;
                });
            } finally {
                cache.releaseLock(lockKey, token);
            }
        }

        long deadline = System.nanoTime() + rebuildWait.toNanos();
        do {
            Optional<List<MemoryScopeEntry>> filled = findScopeEntries(scopeKey);
            if (filled.isPresent()) return filled.get();
            if (rebuildWait.isZero()) break;
            try {
                Thread.sleep(Math.min(20, Math.max(1, rebuildWait.toMillis())));
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                break;
            }
        } while (System.nanoTime() < deadline);
        return List.of();
    }

    public void cacheScopeEntries(String scopeKey, List<MemoryScopeEntry> entries) {
        if (enabled) cache.put(scopeIndexKey(scopeKey), List.copyOf(entries), jitteredIndexTtl());
    }

    public Optional<MemoryVectorValue> findVector(UUID memoryId, long revision) {
        if (!enabled) return Optional.empty();
        return cache.get(vectorKey(memoryId, revision), MemoryVectorValue.class);
    }

    public Map<UUID, MemoryVectorValue> findVectors(List<MemoryScopeEntry> entries) {
        if (!enabled || entries.isEmpty()) return Map.of();
        Map<String, MemoryScopeEntry> byKey = entries.stream().collect(java.util.stream.Collectors.toMap(
                entry -> vectorKey(entry.memoryId(), entry.revision()), entry -> entry, (left, right) -> left
        ));
        Map<String, MemoryVectorValue> cached = cache.getAll(byKey.keySet(), MemoryVectorValue.class);
        java.util.Map<UUID, MemoryVectorValue> result = new java.util.HashMap<>();
        cached.forEach((key, vector) -> result.put(byKey.get(key).memoryId(), vector));
        return Map.copyOf(result);
    }

    public void cacheVector(UUID memoryId, long revision, MemoryVectorValue vector) {
        if (enabled) cache.put(vectorKey(memoryId, revision), vector, vectorTtl);
    }

    /** 索引事务提交成功后再发布向量，避免 Redis 暂时领先于 MySQL。 */
    public void cacheVectorAfterCommit(UUID memoryId, long revision, MemoryVectorValue vector) {
        if (!enabled) return;
        Runnable write = () -> cacheVector(memoryId, revision, vector);
        if (TransactionSynchronizationManager.isSynchronizationActive()
                && TransactionSynchronizationManager.isActualTransactionActive()) {
            TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                @Override
                public void afterCommit() {
                    write.run();
                }
            });
            return;
        }
        write.run();
    }

    public Optional<MemoryContent> findContent(UUID memoryId, long revision) {
        if (!enabled) return Optional.empty();
        return cache.get(contentKey(memoryId, revision), MemoryContent.class);
    }

    public Map<UUID, MemoryContent> findContents(List<MemoryScopeEntry> entries) {
        if (!enabled || entries.isEmpty()) return Map.of();
        Map<String, MemoryScopeEntry> byKey = entries.stream().collect(java.util.stream.Collectors.toMap(
                entry -> contentKey(entry.memoryId(), entry.revision()), entry -> entry, (left, right) -> left
        ));
        Map<String, MemoryContent> cached = cache.getAll(byKey.keySet(), MemoryContent.class);
        java.util.Map<UUID, MemoryContent> result = new java.util.HashMap<>();
        cached.forEach((key, content) -> result.put(byKey.get(key).memoryId(), content));
        return Map.copyOf(result);
    }

    public void cacheContent(UUID memoryId, long revision, MemoryContent content) {
        if (enabled) cache.put(contentKey(memoryId, revision), content, contentTtl);
    }

    /**
     * 只在成功提交后驱逐作用域索引，避免回滚事务错误地清空仍有效的缓存。
     */
    public void invalidateScopeAfterCommit(String scopeKey) {
        if (!enabled) return;
        Runnable eviction = () -> cache.evict(scopeIndexKey(scopeKey));
        if (TransactionSynchronizationManager.isSynchronizationActive()
                && TransactionSynchronizationManager.isActualTransactionActive()) {
            TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                @Override
                public void afterCommit() {
                    eviction.run();
                }
            });
            return;
        }
        eviction.run();
    }

    private String scopeIndexKey(String scopeKey) {
        if ("GLOBAL".equals(scopeKey)) return "memory:index:global:" + embeddingProfile;
        if ("HISTORICAL_SWE_BENCH".equals(scopeKey)) {
            return "memory:index:historical-swe-bench:" + embeddingProfile;
        }
        String repositoryId = scopeKey != null && scopeKey.startsWith("REPOSITORY:")
                ? scopeKey.substring("REPOSITORY:".length()) : normalizedProfile(scopeKey);
        return "memory:index:repo:" + repositoryId + ":" + embeddingProfile;
    }

    private String vectorKey(UUID memoryId, long revision) {
        return "memory:vector:" + memoryId + ":" + revision + ":" + embeddingProfile;
    }

    private String contentKey(UUID memoryId, long revision) {
        return "memory:content:" + memoryId + ":" + revision;
    }

    private Duration jitteredIndexTtl() {
        long millis = indexTtl.toMillis();
        long jitter = Math.max(1, millis / 5);
        return Duration.ofMillis(millis + ThreadLocalRandom.current().nextLong(jitter));
    }

    private static String normalizedProfile(String value) {
        String normalized = value == null ? "" : value.strip();
        if (normalized.isEmpty()) return "memory-retrieval-v1";
        return normalized.replaceAll("[^A-Za-z0-9._-]", "_");
    }
}
