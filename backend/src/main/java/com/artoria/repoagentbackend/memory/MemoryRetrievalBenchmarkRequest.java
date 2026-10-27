package com.artoria.repoagentbackend.memory;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

import java.util.UUID;

/** 仅供受保护的开发基准入口使用，排除问题 Embedding 的耗时。 */
public record MemoryRetrievalBenchmarkRequest(
        @NotNull UUID taskId,
        @NotBlank @Size(max = 12_000) String query,
        @Size(max = 64) String languageName,
        @Size(max = 128) String frameworkName,
        @Size(max = 128) String problemType,
        @Min(0) @Max(20) Integer warmupIterations,
        @Min(3) @Max(200) Integer iterations
) {
    public int effectiveWarmupIterations() {
        return warmupIterations == null ? 5 : warmupIterations;
    }

    public int effectiveIterations() {
        return iterations == null ? 30 : iterations;
    }

    MemorySearchRequest toSearchRequest() {
        return new MemorySearchRequest(
                taskId, query, languageName, frameworkName, problemType, 4, false
        );
    }
}
