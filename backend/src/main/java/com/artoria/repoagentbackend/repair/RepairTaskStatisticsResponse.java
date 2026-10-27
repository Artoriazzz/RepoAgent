package com.artoria.repoagentbackend.repair;

import java.util.Map;

public record RepairTaskStatisticsResponse(
        long total,
        Map<RepairTaskStatus, Long> byStatus,
        Map<BenchmarkEvaluationStatus, Long> byEvaluationStatus,
        long emptyPatchCount,
        long totalDurationMs,
        long totalTokens
) {
}
