package com.artoria.repoagentbackend.memory;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

class MemoryScorerTest {
    @Test
    void metadataAndRelevantTermsRaiseLexicalScore() {
        SemanticMemory memory = new SemanticMemory(
                MemoryScopeType.GLOBAL, null, "GLOBAL", "fingerprint",
                new MemoryContent(
                        "Preserve parser contracts", "Validate parser output boundaries",
                        "Reader/writer parser boundary violates a roundtrip contract",
                        "Python parser changes that affect reader and writer behavior",
                        "Change the shared parser contract and test round trips",
                        "Run reader writer and roundtrip tests", "Do not patch only the writer",
                        "Python", "pandas", "parser-contract", null
                )
        );
        MemorySearchRequest matching = new MemorySearchRequest(
                java.util.UUID.randomUUID(), "parser reader roundtrip", "Python", "pandas",
                "parser-contract", 4, true
        );
        MemorySearchRequest unrelated = new MemorySearchRequest(
                java.util.UUID.randomUUID(), "CSS button color", "JavaScript", "Vue",
                "frontend-style", 4, true
        );

        assertThat(MemoryScorer.lexical(matching.query(), memory, matching))
                .isGreaterThan(MemoryScorer.lexical(unrelated.query(), memory, unrelated));
    }

    @Test
    void cosineRejectsMismatchedDimensions() {
        assertThat(MemoryScorer.cosine(List.of(1.0, 0.0), List.of(1.0))).isZero();
        assertThat(MemoryScorer.cosine(List.of(1.0, 0.0), List.of(1.0, 0.0))).isEqualTo(1.0);
        assertThat(MemoryScorer.cosine(List.of(1.0, 0.0), List.of(0.0, 1.0))).isZero();
    }
}
