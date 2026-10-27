package com.artoria.repoagentbackend.memory;

import java.util.List;

public record MemorySearchResponse(
        String embeddingModel,
        boolean vectorSearchUsed,
        MemoryRetrievalStatus retrievalStatus,
        List<MemorySearchHit> hits
) {
}
