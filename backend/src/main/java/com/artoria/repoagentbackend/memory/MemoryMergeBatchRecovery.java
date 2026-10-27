package com.artoria.repoagentbackend.memory;

import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/** 释放崩溃或超时 Memory Agent 遗留的批次，提案会回到待处理状态。 */
@Component
public class MemoryMergeBatchRecovery {
    private final MemoryMergeBatchService batches;

    public MemoryMergeBatchRecovery(MemoryMergeBatchService batches) {
        this.batches = batches;
    }

    @Scheduled(fixedDelayString = "${app.memory.merge.recovery-interval-millis:30000}")
    public void recover() {
        batches.recoverExpiredBatches();
    }
}
