package com.artoria.repoagentbackend.memory;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record MemoryContent(
        @NotBlank @Size(max = 255) String title,
        @NotBlank String summary,
        @Size(max = 2000) String retrievalText,
        @NotBlank String applicability,
        @NotBlank String repairStrategy,
        String verificationStrategy,
        String antiPatterns,
        @Size(max = 64) String languageName,
        @Size(max = 128) String frameworkName,
        @Size(max = 128) String problemType,
        @Size(max = 128) String dependencyRange
) {
    public MemoryContent(
            String title,
            String summary,
            String applicability,
            String repairStrategy,
            String verificationStrategy,
            String antiPatterns,
            String languageName,
            String frameworkName,
            String problemType,
            String dependencyRange
    ) {
        this(title, summary, null, applicability, repairStrategy, verificationStrategy, antiPatterns,
                languageName, frameworkName, problemType, dependencyRange);
    }

    /** 兼容历史记忆与旧 Worker：尚未提炼检索描述时，以摘要作为临时检索语料。 */
    public String effectiveRetrievalText() {
        return retrievalText == null || retrievalText.isBlank() ? summary : retrievalText;
    }
}
