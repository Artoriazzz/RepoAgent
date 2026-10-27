package com.artoria.repoagentbackend.memory;

import java.util.UUID;

public record MemoryMergeBatchCompletionResponse(
        UUID batchId,
        MemoryMergeBatchStatus status,
        UUID memoryId,
        Long revision,
        String detail
) {
}
