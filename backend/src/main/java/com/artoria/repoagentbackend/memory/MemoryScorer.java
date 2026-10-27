package com.artoria.repoagentbackend.memory;

import java.text.Normalizer;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.stream.Collectors;

final class MemoryScorer {
    private MemoryScorer() {
    }

    static double lexical(String query, SemanticMemory memory, MemorySearchRequest filters) {
        Set<String> queryTokens = tokens(query);
        Set<String> memoryTokens = tokens(memory.embeddingText());
        long matches = queryTokens.stream().filter(memoryTokens::contains).count();
        double score = queryTokens.isEmpty() ? 0 : (double) matches / queryTokens.size();
        score += metadataBoost(filters.languageName(), memory.getLanguageName());
        score += metadataBoost(filters.frameworkName(), memory.getFrameworkName());
        score += metadataBoost(filters.problemType(), memory.getProblemType());
        return Math.min(1, score);
    }

    static double cosine(List<Double> left, List<Double> right) {
        if (left == null || right == null || left.size() != right.size() || left.isEmpty()) return 0;
        double dot = 0;
        double leftNorm = 0;
        double rightNorm = 0;
        for (int index = 0; index < left.size(); index++) {
            double l = left.get(index);
            double r = right.get(index);
            dot += l * r;
            leftNorm += l * l;
            rightNorm += r * r;
        }
        if (leftNorm == 0 || rightNorm == 0) return 0;
        return Math.max(0, Math.min(1, dot / (Math.sqrt(leftNorm) * Math.sqrt(rightNorm))));
    }

    private static double metadataBoost(String expected, String actual) {
        if (expected == null || expected.isBlank()) return 0;
        return expected.equalsIgnoreCase(actual == null ? "" : actual) ? 0.12 : -0.08;
    }

    private static Set<String> tokens(String value) {
        String normalized = Normalizer.normalize(value == null ? "" : value, Normalizer.Form.NFKC)
                .toLowerCase(Locale.ROOT);
        Set<String> result = Arrays.stream(normalized.split("[^\\p{L}\\p{N}_]+"))
                .filter(token -> token.length() > 1)
                .collect(Collectors.toCollection(HashSet::new));
        String compactCjk = normalized.replaceAll("[^\\p{IsHan}]", "");
        for (int index = 0; index + 1 < compactCjk.length(); index++) {
            result.add(compactCjk.substring(index, index + 2));
        }
        return result;
    }
}
