package com.artoria.repoagentbackend.memory;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;

import java.time.Duration;
import java.time.Instant;
import java.util.Optional;
import java.util.UUID;

@Service
public class MemoryIndexService {
    private final MemoryIndexJobJpaRepository jobs;
    private final SemanticMemoryJpaRepository memories;
    private final MemoryEmbeddingClient embeddings;
    private final MemoryRagCache ragCache;
    private final MemoryQdrantStore qdrant;
    private final ObjectMapper objectMapper;
    private final TransactionTemplate transactions;
    private final int maxAttempts;
    private final Duration claimTimeout;

    public MemoryIndexService(
            MemoryIndexJobJpaRepository jobs,
            SemanticMemoryJpaRepository memories,
            MemoryEmbeddingClient embeddings,
            MemoryRagCache ragCache,
            MemoryQdrantStore qdrant,
            ObjectMapper objectMapper,
            PlatformTransactionManager transactionManager,
            @Value("${app.memory.embedding.max-attempts:8}") int maxAttempts,
            @Value("${app.memory.embedding.claim-timeout-seconds:60}") long claimTimeoutSeconds
    ) {
        this.jobs = jobs;
        this.memories = memories;
        this.embeddings = embeddings;
        this.ragCache = ragCache;
        this.qdrant = qdrant;
        this.objectMapper = objectMapper;
        this.transactions = new TransactionTemplate(transactionManager);
        this.maxAttempts = maxAttempts;
        this.claimTimeout = Duration.ofSeconds(claimTimeoutSeconds);
    }

    public void process(UUID jobId) {
        MemoryIndexWork work = transactions.execute(status -> claim(jobId));
        if (work == null) return;
        try {
            Optional<MemoryEmbeddingResult> result = embeddings.embed(work.text());
            if (result.isEmpty()) {
                fail(jobId, "Embedding provider is disabled");
                return;
            }
            MemoryEmbeddingResult embedding = result.get();
            String vectorJson = objectMapper.writeValueAsString(embedding.vector());
            transactions.executeWithoutResult(status -> complete(jobId, work, embedding, vectorJson));
        } catch (Exception error) {
            fail(jobId, error.getMessage() == null ? error.getClass().getSimpleName() : error.getMessage());
        }
    }

    public int recoverStaleClaims() {
        Instant now = Instant.now();
        Integer recovered = transactions.execute(status -> jobs.recoverStale(
                MemoryIndexJobStatus.PROCESSING, MemoryIndexJobStatus.FAILED,
                now.minus(claimTimeout), now, "Embedding index claim expired"
        ));
        return recovered == null ? 0 : recovered;
    }

    /**
     * 新 profile 只会使用新向量。这里渐进地为历史记忆补充重建任务，避免一次性扫全表。
     */
    @Transactional
    public int enqueueMissingCurrentProfile(int batchSize) {
        int created = 0;
        for (SemanticMemory memory : memories.findNeedingEmbeddingProfile(
                MemoryStatus.ACTIVE, embeddings.profile(), PageRequest.of(0, batchSize)
        )) {
            if (jobs.findByMemoryIdAndMemoryRevisionAndEmbeddingProfile(
                    memory.getId(), memory.getVersion(), embeddings.profile()
            ).isEmpty()) {
                jobs.save(new MemoryIndexJob(memory.getId(), memory.getVersion(), embeddings.profile()));
                created++;
            }
        }
        return created;
    }

    private MemoryIndexWork claim(UUID jobId) {
        MemoryIndexJob job = jobs.findByIdForUpdate(jobId).orElse(null);
        if (job == null || !job.claim()) return null;
        SemanticMemory memory = memories.findById(job.getMemoryId()).orElse(null);
        if (memory == null || memory.getVersion() != job.getMemoryRevision()
                || !embeddings.profile().equals(job.getEmbeddingProfile())) {
            job.markIndexed();
            return null;
        }
        return new MemoryIndexWork(memory.getId(), memory.getVersion(), job.getEmbeddingProfile(), memory.embeddingText());
    }

    private void complete(
            UUID jobId, MemoryIndexWork work, MemoryEmbeddingResult embedding, String vectorJson
    ) {
        MemoryIndexJob job = jobs.findByIdForUpdate(jobId).orElse(null);
        if (job == null || job.getStatus() != MemoryIndexJobStatus.PROCESSING) return;
        SemanticMemory memory = memories.findByIdForUpdate(work.memoryId()).orElse(null);
        if (memory != null && memory.getVersion() == work.revision()) {
            memory.updateEmbedding(
                    vectorJson, embedding.model(), work.profile(), embedding.vector().size(), work.revision()
            );
            // 在持有记忆行锁时更新 Qdrant，内容修改与向量副本不会发生交叉覆盖。
            qdrant.upsert(memory, new MemoryVectorValue(
                    embedding.model(), embedding.vector().size(), embedding.vector()
            ));
            ragCache.cacheVectorAfterCommit(memory.getId(), memory.getVersion(), new MemoryVectorValue(
                    embedding.model(), embedding.vector().size(), embedding.vector()
            ));
        }
        job.markIndexed();
    }

    private void fail(UUID jobId, String reason) {
        transactions.executeWithoutResult(status -> jobs.findByIdForUpdate(jobId)
                .filter(job -> job.getStatus() == MemoryIndexJobStatus.PROCESSING)
                .ifPresent(job -> job.markFailed(reason, maxAttempts)));
    }

    private record MemoryIndexWork(UUID memoryId, long revision, String profile, String text) {
    }
}
