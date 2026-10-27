package com.artoria.repoagentbackend.memory;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.data.domain.PageRequest;

import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * MySQL 是记忆主库，Qdrant 是可重建的向量副本。
 *
 * <p>索引任务负责实时写入；该低频任务只做兜底对账。它轮询当前 embedding 的记忆并重写
 * 版本化 point，因此短暂的 Qdrant 故障、旧 Worker 写入或清理失败都不会永久留下脏数据。</p>
 */
@Component
public class MemoryQdrantReconciler {
    private static final Logger log = LoggerFactory.getLogger(MemoryQdrantReconciler.class);

    private final SemanticMemoryJpaRepository memories;
    private final MemoryQdrantStore qdrant;
    private final boolean reconcileEnabled;
    private final String embeddingProfile;
    private final int batchSize;
    private final AtomicInteger page = new AtomicInteger();

    public MemoryQdrantReconciler(
            SemanticMemoryJpaRepository memories,
            MemoryQdrantStore qdrant,
            @Value("${app.memory.qdrant.reconcile-enabled:true}") boolean reconcileEnabled,
            @Value("${app.memory.embedding.profile:memory-retrieval-v1}") String embeddingProfile,
            @Value("${app.memory.qdrant.reconcile-batch-size:64}") int batchSize
    ) {
        this.memories = memories;
        this.qdrant = qdrant;
        this.reconcileEnabled = reconcileEnabled;
        this.embeddingProfile = embeddingProfile;
        this.batchSize = Math.max(1, batchSize);
    }

    @Scheduled(
            fixedDelayString = "${app.memory.qdrant.reconcile-interval-millis:900000}",
            initialDelayString = "${app.memory.qdrant.reconcile-interval-millis:900000}"
    )
    public void reconcileCurrentPage() {
        if (!reconcileEnabled || !qdrant.isEnabled()) return;
        try {
            int currentPage = page.get();
            List<SemanticMemory> batch = memories.findWithCurrentEmbedding(
                    MemoryStatus.ACTIVE, embeddingProfile, PageRequest.of(currentPage, batchSize)
            );
            if (batch.isEmpty()) {
                page.set(0);
                return;
            }
            int indexed = qdrant.upsertPersisted(batch);
            if (batch.size() < batchSize) page.set(0);
            else page.compareAndSet(currentPage, currentPage + 1);
            log.debug("Qdrant memory reconciliation wrote {} current points", indexed);
        } catch (RuntimeException error) {
            // 召回端的熔断器会快速降级；对账在下一个周期重试，不能占住调度线程。
            log.warn("Qdrant memory reconciliation failed: {}", error.getMessage());
        }
    }
}
