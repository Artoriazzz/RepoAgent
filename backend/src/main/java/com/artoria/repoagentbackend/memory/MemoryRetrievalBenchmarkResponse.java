package com.artoria.repoagentbackend.memory;

/** 检索层基准结果；不包含一次性生成问题向量的时间。 */
public record MemoryRetrievalBenchmarkResponse(
        String backend,
        int warmupIterations,
        int iterations,
        double averageMillis,
        double p50Millis,
        double p95Millis,
        double minMillis,
        double maxMillis,
        boolean vectorSearchUsed,
        int hitCount,
        String firstMatchReason
) {
}
