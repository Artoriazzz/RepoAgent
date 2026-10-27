package com.artoria.repoagentbackend.memory;

import com.artoria.repoagentbackend.common.BadRequestException;
import com.artoria.repoagentbackend.common.ForbiddenException;
import com.artoria.repoagentbackend.repair.RepairWorkerTokenVerifier;
import jakarta.validation.Valid;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * 开发期检索基准入口。默认不注册，且仍要求 Worker Token，避免它成为生产环境的放大接口。
 */
@RestController
@RequestMapping("/internal/memory-benchmark")
@ConditionalOnProperty(name = "app.memory.benchmark.enabled", havingValue = "true")
public class MemoryRetrievalBenchmarkController {
    private final MemoryService memories;
    private final MemoryEmbeddingClient embeddings;
    private final RepairWorkerTokenVerifier tokenVerifier;
    private final boolean qdrantEnabled;
    private final boolean redisCacheEnabled;

    public MemoryRetrievalBenchmarkController(
            MemoryService memories,
            MemoryEmbeddingClient embeddings,
            RepairWorkerTokenVerifier tokenVerifier,
            @Value("${app.memory.qdrant.enabled:false}") boolean qdrantEnabled,
            @Value("${app.memory.rag-cache.enabled:true}") boolean redisCacheEnabled
    ) {
        this.memories = memories;
        this.embeddings = embeddings;
        this.tokenVerifier = tokenVerifier;
        this.qdrantEnabled = qdrantEnabled;
        this.redisCacheEnabled = redisCacheEnabled;
    }

    @PostMapping("/search")
    public MemoryRetrievalBenchmarkResponse benchmark(
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody MemoryRetrievalBenchmarkRequest request
    ) {
        if (!tokenVerifier.isValid(token)) {
            throw new ForbiddenException("Invalid RepoAgent worker token");
        }
        MemorySearchRequest searchRequest = request.toSearchRequest();
        MemoryEmbeddingResult embedding = embeddings.embed(searchRequest.embeddingText())
                .orElseThrow(() -> new BadRequestException("Embedding provider is required for retrieval benchmarking"));

        for (int index = 0; index < request.effectiveWarmupIterations(); index++) {
            memories.searchWithEmbedding(searchRequest, embedding);
        }

        List<Double> samples = new ArrayList<>();
        MemorySearchResponse last = null;
        for (int index = 0; index < request.effectiveIterations(); index++) {
            long startedAt = System.nanoTime();
            last = memories.searchWithEmbedding(searchRequest, embedding);
            samples.add((System.nanoTime() - startedAt) / 1_000_000d);
        }
        samples.sort(Comparator.naturalOrder());
        return new MemoryRetrievalBenchmarkResponse(
                backend(), request.effectiveWarmupIterations(), request.effectiveIterations(),
                round(samples.stream().mapToDouble(Double::doubleValue).average().orElse(0)),
                percentile(samples, 0.50d), percentile(samples, 0.95d), round(samples.get(0)),
                round(samples.get(samples.size() - 1)), last != null && last.vectorSearchUsed(),
                last == null ? 0 : last.hits().size(),
                last == null || last.hits().isEmpty() ? null : last.hits().get(0).matchReason()
        );
    }

    private String backend() {
        if (qdrantEnabled) return "qdrant";
        return redisCacheEnabled ? "redis-3-tier" : "mysql-no-cache";
    }

    private static double percentile(List<Double> sorted, double percentile) {
        int index = Math.max(0, (int) Math.ceil(sorted.size() * percentile) - 1);
        return round(sorted.get(index));
    }

    private static double round(double value) {
        return Math.round(value * 100d) / 100d;
    }
}
