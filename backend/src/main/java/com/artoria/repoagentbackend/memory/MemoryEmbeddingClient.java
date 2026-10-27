package com.artoria.repoagentbackend.memory;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpHeaders;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;

@Component
public class MemoryEmbeddingClient {
    private final boolean enabled;
    private final String model;
    private final String profile;
    private final RestClient client;

    public MemoryEmbeddingClient(
            @Value("${app.memory.embedding.enabled:false}") boolean enabled,
            @Value("${app.memory.embedding.base-url:https://api.openai.com/v1}") String baseUrl,
            @Value("${app.memory.embedding.api-key:}") String apiKey,
            @Value("${app.memory.embedding.model:text-embedding-3-small}") String model,
            @Value("${app.memory.embedding.profile:memory-retrieval-v1}") String profile,
            @Value("${app.memory.embedding.timeout-millis:15000}") int timeoutMillis
    ) {
        this.enabled = enabled && apiKey != null && !apiKey.isBlank();
        this.model = model;
        this.profile = normalizeProfile(profile);
        SimpleClientHttpRequestFactory requestFactory = new SimpleClientHttpRequestFactory();
        requestFactory.setConnectTimeout(timeoutMillis);
        requestFactory.setReadTimeout(timeoutMillis);
        RestClient.Builder builder = RestClient.builder().baseUrl(stripTrailingSlash(baseUrl) + "/")
                .requestFactory(requestFactory);
        if (apiKey != null && !apiKey.isBlank()) {
            builder.defaultHeader(HttpHeaders.AUTHORIZATION, "Bearer " + apiKey);
        }
        this.client = builder.build();
    }

    public Optional<MemoryEmbeddingResult> embed(String text) {
        if (!enabled) return Optional.empty();
        String normalized = text == null ? "" : text.strip();
        JsonNode response = client.post()
                .uri("embeddings")
                .body(Map.of("model", model, "input", normalized))
                .retrieve()
                .body(JsonNode.class);
        JsonNode embedding = response == null ? null : response.path("data").path(0).path("embedding");
        if (embedding == null || !embedding.isArray() || embedding.isEmpty()) {
            throw new IllegalStateException("Embedding provider returned no vector");
        }
        List<Double> vector = new ArrayList<>(embedding.size());
        embedding.forEach(value -> vector.add(value.asDouble()));
        String actualModel = response.path("model").asText(model);
        return Optional.of(new MemoryEmbeddingResult(actualModel, List.copyOf(vector)));
    }

    public boolean isEnabled() {
        return enabled;
    }

    public String model() {
        return model;
    }

    /** 模型与编码文本规则共同决定的版本标识，切换时不能混用旧向量。 */
    public String profile() {
        return profile;
    }

    private static String stripTrailingSlash(String value) {
        return value.endsWith("/") ? value.substring(0, value.length() - 1) : value;
    }

    private static String normalizeProfile(String value) {
        String normalized = value == null ? "" : value.strip();
        if (normalized.isEmpty()) return "memory-retrieval-v1";
        return normalized.replaceAll("[^A-Za-z0-9._-]", "_");
    }

}
