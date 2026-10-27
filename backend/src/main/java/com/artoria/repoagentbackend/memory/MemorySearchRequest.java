package com.artoria.repoagentbackend.memory;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

import java.util.UUID;

public record MemorySearchRequest(
        @NotNull UUID taskId,
        @NotBlank @Size(max = 12000) String query,
        @Size(max = 64) String languageName,
        @Size(max = 128) String frameworkName,
        @Size(max = 128) String problemType,
        @Min(1) @Max(10) Integer limit,
        Boolean trackRecall,
        @Size(max = 512) String object,
        @Size(max = 512) String operation,
        @Size(max = 2000) String symptom,
        @Size(max = 2000) String expectedBehavior,
        @Size(max = 2000) String triggerConditions
) {
    public MemorySearchRequest(
            UUID taskId, String query, String languageName, String frameworkName, String problemType,
            Integer limit, Boolean trackRecall
    ) {
        this(taskId, query, languageName, frameworkName, problemType, limit, trackRecall,
                null, null, null, null, null);
    }

    public int effectiveLimit() {
        return limit == null ? 4 : limit;
    }

    public boolean shouldTrackRecall() {
        return trackRecall == null || trackRecall;
    }

    /**
     * 有明确问题特征时只嵌入稳定的语义约束；首次召回仍退化为 Issue 文本。
     */
    public String embeddingText() {
        StringBuilder text = new StringBuilder();
        appendFeature(text, "object", object);
        appendFeature(text, "operation", operation);
        appendFeature(text, "symptom", symptom);
        appendFeature(text, "expected behavior", expectedBehavior);
        appendFeature(text, "trigger conditions", triggerConditions);
        return text.isEmpty() ? query : text.toString();
    }

    private static void appendFeature(StringBuilder text, String name, String value) {
        if (value == null || value.isBlank()) return;
        if (!text.isEmpty()) text.append('\n');
        text.append(name).append(": ").append(value.strip());
    }
}
