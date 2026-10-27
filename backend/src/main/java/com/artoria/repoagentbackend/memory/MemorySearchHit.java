package com.artoria.repoagentbackend.memory;

public record MemorySearchHit(
        MemoryResponse memory,
        double score,
        double lexicalScore,
        Double vectorScore,
        String matchReason
) {
}
