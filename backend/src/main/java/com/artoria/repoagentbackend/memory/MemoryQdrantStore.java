package com.artoria.repoagentbackend.memory;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.Semaphore;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Qdrant 是记忆向量检索的可选副本，MySQL 仍然保存主数据、版本和审计记录。
 *
 * <p>每条记忆只占一个 point，payload 同时保存过滤字段和完整记忆正文。这样检索命中后
 * 不需要再批量搬运候选向量与正文；调用方只会对返回的少量命中做版本校验。</p>
 */
@Component
public class MemoryQdrantStore {
    private static final Logger log = LoggerFactory.getLogger(MemoryQdrantStore.class);
    private static final TypeReference<List<Double>> VECTOR_TYPE = new TypeReference<>() { };

    private final boolean enabled;
    private final String collection;
    private final String embeddingProfile;
    private final int candidateMultiplier;
    private final int failureThreshold;
    private final long circuitCooldownMillis;
    private final Semaphore queryPermits;
    private final RestClient client;
    private final ObjectMapper objectMapper;
    private final AtomicInteger consecutiveFailures = new AtomicInteger();
    private final AtomicLong circuitOpenUntilMillis = new AtomicLong();
    private final AtomicBoolean recoveryProbeInFlight = new AtomicBoolean();
    private Integer collectionDimensions;

    public MemoryQdrantStore(
            ObjectMapper objectMapper,
            @Value("${app.memory.qdrant.enabled:false}") boolean enabled,
            @Value("${app.memory.qdrant.base-url:http://127.0.0.1:6333}") String baseUrl,
            @Value("${app.memory.qdrant.collection:repoagent_memory_v1}") String collection,
            @Value("${app.memory.embedding.profile:memory-retrieval-v1}") String embeddingProfile,
            @Value("${app.memory.qdrant.timeout-millis:3000}") int timeoutMillis,
            @Value("${app.memory.qdrant.candidate-multiplier:4}") int candidateMultiplier,
            @Value("${app.memory.qdrant.circuit-failure-threshold:3}") int failureThreshold,
            @Value("${app.memory.qdrant.circuit-cooldown-millis:10000}") long circuitCooldownMillis,
            @Value("${app.memory.qdrant.max-concurrent-queries:32}") int maxConcurrentQueries
    ) {
        this.enabled = enabled;
        this.collection = normalizeCollection(collection);
        this.embeddingProfile = normalizeProfile(embeddingProfile);
        this.candidateMultiplier = Math.max(1, candidateMultiplier);
        this.failureThreshold = Math.max(1, failureThreshold);
        this.circuitCooldownMillis = Math.max(1000, circuitCooldownMillis);
        this.queryPermits = new Semaphore(Math.max(1, maxConcurrentQueries));
        this.objectMapper = objectMapper;

        SimpleClientHttpRequestFactory requestFactory = new SimpleClientHttpRequestFactory();
        requestFactory.setConnectTimeout(Math.max(1, timeoutMillis));
        requestFactory.setReadTimeout(Math.max(1, timeoutMillis));
        this.client = RestClient.builder()
                .baseUrl(stripTrailingSlash(baseUrl) + "/")
                .requestFactory(requestFactory)
                .build();
    }

    public boolean isEnabled() {
        return enabled;
    }

    /**
     * 由索引任务在锁定记忆行的事务内调用。Qdrant 不可用时抛出异常，让索引任务按既有
     * 重试策略恢复，避免 MySQL 已标记索引完成而 Qdrant 没有对应 point。
     */
    public void upsert(SemanticMemory memory, MemoryVectorValue vector) {
        if (!enabled) return;
        upsertDocuments(List.of(new MemoryVectorDocument(memory, vector)));
    }

    /** 启动回填使用已持久化的嵌入，避免为了建立 Qdrant 副本重新调用 embedding 模型。 */
    public int upsertPersisted(List<SemanticMemory> memories) {
        if (!enabled || memories.isEmpty()) return 0;
        List<MemoryVectorDocument> documents = new ArrayList<>();
        for (SemanticMemory memory : memories) {
            storedVector(memory).ifPresent(vector -> documents.add(new MemoryVectorDocument(memory, vector)));
        }
        if (!documents.isEmpty()) upsertDocuments(documents);
        return documents.size();
    }

    public MemoryQdrantQueryResult search(
            MemoryEmbeddingResult queryEmbedding, List<String> scopeKeys, int requestedLimit
    ) {
        if (!enabled) return MemoryQdrantQueryResult.disabled();
        if (scopeKeys.isEmpty() || queryEmbedding.vector().isEmpty()) {
            return MemoryQdrantQueryResult.success(List.of());
        }
        QueryPermit permit = tryAcquireQueryPermit();
        if (permit == null) {
            return MemoryQdrantQueryResult.unavailable("Qdrant circuit is open or query capacity is exhausted");
        }
        try {
            // 首次只有查询、尚未产生任何成功记忆时也要建立空 collection；否则 404 会被误判为
            // 服务故障并打开熔断器。
            ensureCollection(queryEmbedding.vector().size());
            Map<String, Object> request = new LinkedHashMap<>();
            request.put("query", queryEmbedding.vector());
            request.put("limit", Math.max(1, requestedLimit) * candidateMultiplier);
            request.put("with_payload", true);
            request.put("with_vector", false);
            request.put("filter", filter(scopeKeys, queryEmbedding.model()));
            JsonNode response = client.post()
                    .uri("collections/{collection}/points/query", collection)
                    .body(request)
                    .retrieve()
                    .body(JsonNode.class);
            List<MemoryQdrantSearchHit> hits = parseSearchHits(response);
            recordQuerySuccess(permit.recoveryProbe());
            return MemoryQdrantQueryResult.success(hits);
        } catch (RestClientResponseException error) {
            recordQueryFailure(permit.recoveryProbe(), error.getMessage());
            return MemoryQdrantQueryResult.unavailable("Qdrant returned HTTP " + error.getStatusCode().value());
        } catch (RuntimeException error) {
            recordQueryFailure(permit.recoveryProbe(), error.getMessage());
            return MemoryQdrantQueryResult.unavailable("Qdrant query failed");
        } finally {
            queryPermits.release();
        }
    }

    private void upsertDocuments(List<MemoryVectorDocument> documents) {
        Map<Integer, List<MemoryVectorDocument>> byDimensions = new LinkedHashMap<>();
        for (MemoryVectorDocument document : documents) {
            byDimensions.computeIfAbsent(document.vector().dimensions(), ignored -> new ArrayList<>()).add(document);
        }
        for (Map.Entry<Integer, List<MemoryVectorDocument>> entry : byDimensions.entrySet()) {
            ensureCollection(entry.getKey());
            List<Map<String, Object>> points = new ArrayList<>();
            for (MemoryVectorDocument document : entry.getValue()) {
                Map<String, Object> point = new LinkedHashMap<>();
                point.put("id", pointId(document.memory()).toString());
                point.put("vector", document.vector().vector());
                point.put("payload", payload(document.memory(), document.vector()));
                points.add(point);
            }
            client.put()
                    .uri("collections/{collection}/points?wait=true", collection)
                    .body(Map.of("points", points))
                    .retrieve()
                    .toBodilessEntity();
            for (MemoryVectorDocument document : entry.getValue()) {
                deleteSupersededPoints(document.memory());
            }
        }
    }

    private synchronized void ensureCollection(int dimensions) {
        if (collectionDimensions != null) {
            if (collectionDimensions != dimensions) {
                throw new IllegalStateException("Qdrant collection dimensions do not match the current embedding profile");
            }
            return;
        }
        boolean exists = false;
        try {
            JsonNode response = client.get()
                    .uri("collections/{collection}", collection)
                    .retrieve()
                    .body(JsonNode.class);
            int existingDimensions = response == null ? 0
                    : response.path("result").path("config").path("params").path("vectors").path("size").asInt();
            if (existingDimensions > 0 && existingDimensions != dimensions) {
                throw new IllegalStateException("Qdrant collection has incompatible vector dimensions: " + existingDimensions);
            }
            exists = true;
        } catch (RestClientResponseException error) {
            if (error.getStatusCode().value() != HttpStatus.NOT_FOUND.value()) throw error;
        }
        if (!exists) {
            try {
                client.put()
                        .uri("collections/{collection}", collection)
                        .body(Map.of("vectors", Map.of("size", dimensions, "distance", "Cosine")))
                        .retrieve()
                        .toBodilessEntity();
            } catch (RestClientResponseException error) {
                if (error.getStatusCode().value() != HttpStatus.CONFLICT.value()) throw error;
            }
        }
        createKeywordIndex("scope_key");
        createKeywordIndex("memory_id");
        createKeywordIndex("status");
        createKeywordIndex("embedding_profile");
        createKeywordIndex("embedding_model");
        collectionDimensions = dimensions;
    }

    private void createKeywordIndex(String fieldName) {
        client.put()
                .uri("collections/{collection}/index", collection)
                .body(Map.of("field_name", fieldName, "field_schema", "keyword"))
                .retrieve()
                .toBodilessEntity();
    }

    private void delete(UUID memoryId) {
        client.post()
                .uri("collections/{collection}/points/delete?wait=true", collection)
                .body(Map.of("points", List.of(memoryId.toString())))
                .retrieve()
                .toBodilessEntity();
    }

    /**
     * 新版本已写入后才删除旧索引。旧 Worker 写入其版本化 Point 不会覆盖新版；即使
     * 清理失败，查询端的 revision 校验也会剔除旧 payload，下一次对账还会再次清理。
     */
    private void deleteSupersededPoints(SemanticMemory memory) {
        try {
            Map<String, Object> filter = new LinkedHashMap<>();
            filter.put("must", List.of(
                    condition("memory_id", memory.getId().toString()),
                    condition("embedding_profile", embeddingProfile)
            ));
            filter.put("must_not", List.of(condition("content_revision", memory.getVersion())));
            client.post()
                    .uri("collections/{collection}/points/delete?wait=true", collection)
                    .body(Map.of("filter", filter))
                    .retrieve()
                    .toBodilessEntity();
            // 兼容升级前以 memoryId 作为 point ID 的单版本索引。
            delete(memory.getId());
        } catch (RuntimeException error) {
            log.warn("Failed to clean superseded Qdrant memory points for {}: {}", memory.getId(), error.getMessage());
        }
    }

    private Map<String, Object> payload(SemanticMemory memory, MemoryVectorValue vector) {
        MemoryContent content = memory.content();
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("memory_id", memory.getId().toString());
        payload.put("content_revision", memory.getVersion());
        payload.put("scope_type", memory.getScopeType().name());
        payload.put("scope_key", memory.getScopeKey());
        putIfNotNull(payload, "repository_id", memory.getRepositoryId() == null ? null : memory.getRepositoryId().toString());
        payload.put("status", memory.getStatus().name());
        payload.put("quality_score", memory.getQualityScore());
        payload.put("successful_uses", memory.getSuccessfulUses());
        payload.put("failed_uses", memory.getFailedUses());
        payload.put("embedding_model", vector.model());
        payload.put("embedding_profile", embeddingProfile);
        payload.put("embedding_dimensions", vector.dimensions());
        payload.put("updated_at", memory.getUpdatedAt().toString());

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("title", content.title());
        body.put("summary", content.summary());
        body.put("retrieval_text", content.retrievalText());
        body.put("applicability", content.applicability());
        body.put("repair_strategy", content.repairStrategy());
        putIfNotNull(body, "verification_strategy", content.verificationStrategy());
        putIfNotNull(body, "anti_patterns", content.antiPatterns());
        putIfNotNull(body, "language_name", content.languageName());
        putIfNotNull(body, "framework_name", content.frameworkName());
        putIfNotNull(body, "problem_type", content.problemType());
        putIfNotNull(body, "dependency_range", content.dependencyRange());
        payload.put("content", body);
        return payload;
    }

    private Map<String, Object> filter(List<String> scopeKeys, String embeddingModel) {
        List<Map<String, Object>> scopes = scopeKeys.stream()
                .map(scope -> condition("scope_key", scope))
                .toList();
        return Map.of(
                "must", List.of(
                        condition("status", MemoryStatus.ACTIVE.name()),
                        condition("embedding_profile", embeddingProfile),
                        condition("embedding_model", embeddingModel)
                ),
                "should", scopes
        );
    }

    private List<MemoryQdrantSearchHit> parseSearchHits(JsonNode response) {
        if (response == null || !response.path("result").path("points").isArray()) return List.of();
        List<MemoryQdrantSearchHit> hits = new ArrayList<>();
        for (JsonNode point : response.path("result").path("points")) {
            toMemory(point.path("payload")).ifPresent(memory -> {
                double similarity = point.path("score").asDouble();
                double score = similarity * 0.9d + memory.qualityScore() * 0.1d;
                if (score >= 0.08d) hits.add(new MemoryQdrantSearchHit(memory, round(similarity), round(score)));
            });
        }
        hits.sort(java.util.Comparator.comparingDouble(MemoryQdrantSearchHit::score).reversed());
        return List.copyOf(hits);
    }

    private Optional<MemoryResponse> toMemory(JsonNode payload) {
        try {
            JsonNode content = payload.path("content");
            if (content.isMissingNode()) return Optional.empty();
            UUID id = UUID.fromString(payload.path("memory_id").asText());
            MemoryContent body = new MemoryContent(
                    required(content, "title"), required(content, "summary"), text(content, "retrieval_text"),
                    required(content, "applicability"), required(content, "repair_strategy"),
                    text(content, "verification_strategy"), text(content, "anti_patterns"),
                    text(content, "language_name"), text(content, "framework_name"), text(content, "problem_type"),
                    text(content, "dependency_range")
            );
            String repositoryId = text(payload, "repository_id");
            return Optional.of(new MemoryResponse(
                    id,
                    MemoryScopeType.valueOf(required(payload, "scope_type")),
                    required(payload, "scope_key"),
                    repositoryId == null ? null : UUID.fromString(repositoryId),
                    payload.path("content_revision").asLong(),
                    MemoryStatus.valueOf(required(payload, "status")),
                    payload.path("quality_score").asDouble(),
                    payload.path("successful_uses").asLong(),
                    payload.path("failed_uses").asLong(),
                    body,
                    required(payload, "embedding_model"),
                    required(payload, "embedding_profile"),
                    payload.path("embedding_dimensions").asInt(),
                    payload.path("content_revision").asLong(),
                    Instant.parse(required(payload, "updated_at"))
            ));
        } catch (RuntimeException invalidPayload) {
            log.warn("Ignoring malformed Qdrant memory payload: {}", invalidPayload.getMessage());
            return Optional.empty();
        }
    }

    private Optional<MemoryVectorValue> storedVector(SemanticMemory memory) {
        if (memory.getEmbeddingJson() == null || memory.getEmbeddingRevision() == null
                || memory.getEmbeddingRevision() != memory.getVersion()
                || !embeddingProfile.equals(memory.getEmbeddingProfile())
                || memory.getEmbeddingModel() == null) {
            return Optional.empty();
        }
        try {
            List<Double> vector = objectMapper.readValue(memory.getEmbeddingJson(), VECTOR_TYPE);
            if (vector.isEmpty() || vector.stream().anyMatch(value -> value == null || !Double.isFinite(value))) {
                return Optional.empty();
            }
            return Optional.of(new MemoryVectorValue(memory.getEmbeddingModel(), vector.size(), vector));
        } catch (JsonProcessingException invalidVector) {
            return Optional.empty();
        }
    }

    private QueryPermit tryAcquireQueryPermit() {
        long now = System.currentTimeMillis();
        long openUntil = circuitOpenUntilMillis.get();
        boolean probe = false;
        if (openUntil > now) return null;
        if (openUntil != 0 && !recoveryProbeInFlight.compareAndSet(false, true)) return null;
        probe = openUntil != 0;
        if (!queryPermits.tryAcquire()) {
            if (probe) recoveryProbeInFlight.set(false);
            return null;
        }
        return new QueryPermit(probe);
    }

    private void recordQuerySuccess(boolean recoveryProbe) {
        consecutiveFailures.set(0);
        circuitOpenUntilMillis.set(0);
        if (recoveryProbe) recoveryProbeInFlight.set(false);
    }

    private void recordQueryFailure(boolean recoveryProbe, String reason) {
        int failures = consecutiveFailures.incrementAndGet();
        if (recoveryProbe || failures >= failureThreshold) {
            circuitOpenUntilMillis.set(System.currentTimeMillis() + circuitCooldownMillis);
            log.warn("Opening Qdrant memory circuit after {} failures: {}", failures, reason);
        }
        if (recoveryProbe) recoveryProbeInFlight.set(false);
    }

    private UUID pointId(SemanticMemory memory) {
        String identity = memory.getId() + ":" + memory.getVersion() + ":" + embeddingProfile;
        return UUID.nameUUIDFromBytes(identity.getBytes(StandardCharsets.UTF_8));
    }

    private static Map<String, Object> condition(String key, Object value) {
        return Map.of("key", key, "match", Map.of("value", value));
    }

    private static void putIfNotNull(Map<String, Object> target, String key, Object value) {
        if (value != null) target.put(key, value);
    }

    private static String required(JsonNode node, String field) {
        String value = text(node, field);
        if (value == null || value.isBlank()) throw new IllegalArgumentException("Missing payload field: " + field);
        return value;
    }

    private static String text(JsonNode node, String field) {
        JsonNode value = node.path(field);
        return value.isMissingNode() || value.isNull() ? null : value.asText();
    }

    private static String stripTrailingSlash(String value) {
        return value.endsWith("/") ? value.substring(0, value.length() - 1) : value;
    }

    private static String normalizeProfile(String value) {
        String normalized = value == null ? "" : value.strip();
        if (normalized.isEmpty()) return "memory-retrieval-v1";
        return normalized.replaceAll("[^A-Za-z0-9._-]", "_");
    }

    private static String normalizeCollection(String value) {
        String normalized = value == null ? "" : value.strip().replaceAll("[^A-Za-z0-9_-]", "_");
        return normalized.isEmpty() ? "repoagent_memory_v1" : normalized;
    }

    private static double round(double value) {
        return Math.round(value * 10_000d) / 10_000d;
    }

    private record MemoryVectorDocument(SemanticMemory memory, MemoryVectorValue vector) {
    }

    private record QueryPermit(boolean recoveryProbe) {
    }
}
