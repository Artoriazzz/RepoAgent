package com.artoria.repoagentbackend.memory;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.domain.PageRequest;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.util.Set;
import java.util.UUID;

@Component
@ConditionalOnProperty(name = "app.memory.embedding.enabled", havingValue = "true")
public class MemoryIndexDispatcher {
    private static final Set<MemoryIndexJobStatus> DISPATCHABLE = Set.of(
            MemoryIndexJobStatus.PENDING, MemoryIndexJobStatus.FAILED
    );

    private final MemoryIndexJobJpaRepository jobs;
    private final MemoryIndexService indexer;
    private final int batchSize;

    public MemoryIndexDispatcher(
            MemoryIndexJobJpaRepository jobs,
            MemoryIndexService indexer,
            @Value("${app.memory.embedding.batch-size:10}") int batchSize
    ) {
        this.jobs = jobs;
        this.indexer = indexer;
        this.batchSize = batchSize;
    }

    @Scheduled(fixedDelayString = "${app.memory.embedding.poll-interval-millis:2000}")
    public void dispatch() {
        indexer.recoverStaleClaims();
        indexer.enqueueMissingCurrentProfile(batchSize);
        for (UUID jobId : jobs.findDispatchableIds(
                DISPATCHABLE, Instant.now(), PageRequest.of(0, batchSize))) {
            indexer.process(jobId);
        }
    }
}
