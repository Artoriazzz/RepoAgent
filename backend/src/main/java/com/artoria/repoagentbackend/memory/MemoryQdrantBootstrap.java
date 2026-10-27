package com.artoria.repoagentbackend.memory;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.event.EventListener;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Component;

import java.util.List;

/** 一次性把已存在的 MySQL 向量副本回填到 Qdrant，不重新请求 embedding 服务。 */
@Component
@ConditionalOnProperty(name = "app.memory.qdrant.bootstrap-on-startup", havingValue = "true")
public class MemoryQdrantBootstrap {
    private static final Logger log = LoggerFactory.getLogger(MemoryQdrantBootstrap.class);

    private final SemanticMemoryJpaRepository memories;
    private final MemoryQdrantStore qdrant;
    private final String embeddingProfile;
    private final int batchSize;

    public MemoryQdrantBootstrap(
            SemanticMemoryJpaRepository memories,
            MemoryQdrantStore qdrant,
            @Value("${app.memory.embedding.profile:memory-retrieval-v1}") String embeddingProfile,
            @Value("${app.memory.qdrant.bootstrap-batch-size:64}") int batchSize
    ) {
        this.memories = memories;
        this.qdrant = qdrant;
        this.embeddingProfile = embeddingProfile;
        this.batchSize = Math.max(1, batchSize);
    }

    @EventListener(ApplicationReadyEvent.class)
    public void backfill() {
        if (!qdrant.isEnabled()) return;
        int page = 0;
        int indexed = 0;
        while (true) {
            List<SemanticMemory> batch = memories.findWithCurrentEmbedding(
                    MemoryStatus.ACTIVE, embeddingProfile, PageRequest.of(page, batchSize)
            );
            if (batch.isEmpty()) break;
            indexed += qdrant.upsertPersisted(batch);
            if (batch.size() < batchSize) break;
            page++;
        }
        log.info("Qdrant memory bootstrap completed: {} points", indexed);
    }
}
