package com.artoria.repoagentbackend.memory;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.stream.Stream;

/**
 * 将旧 RepoAgent 文件型经验迁移到 MySQL 语义记忆。
 *
 * <p>旧记录没有当前后端的 RepairTask 和 Observation，因此导入 revision 的
 * sourceObservationId 为空；原始文件名和“已验证 SWE-bench Lite”来源保存在
 * changeReason。导入只接受旧版三类抽象经验，不读取 patch、隐藏测试或原始轨迹。</p>
 */
@Service
public class LegacySWEbenchMemoryImportService {
    private static final Logger log = LoggerFactory.getLogger(LegacySWEbenchMemoryImportService.class);
    /**
     * 历史 benchmark 经验可供真实仓库参考，但不能回流到 SWE-bench 自身评测，
     * 因而不与正常 GLOBAL 作用域混用。
     */
    private static final String HISTORICAL_SWE_BENCH_SCOPE = "HISTORICAL_SWE_BENCH";
    private static final int MAX_SCAN_DEPTH = 6;
    private static final Set<String> LEGACY_PREFIXES = Set.of(
            "repair_recipe_", "repair_episode_", "semantic_insight_"
    );
    private static final Set<String> INLINE_FIELDS = Set.of(
            "problemsignature", "problemsignatures", "inputevidence", "failedattempt",
            "feedback", "successfuloutput", "validation", "applicability", "repairstrategy",
            "verificationcontract", "verificationstrategy"
    );

    private final SemanticMemoryJpaRepository memories;
    private final MemoryRevisionJpaRepository revisions;
    private final MemoryIndexJobJpaRepository indexJobs;
    private final MemoryEmbeddingClient embeddings;

    public LegacySWEbenchMemoryImportService(
            SemanticMemoryJpaRepository memories,
            MemoryRevisionJpaRepository revisions,
            MemoryIndexJobJpaRepository indexJobs,
            MemoryEmbeddingClient embeddings
    ) {
        this.memories = memories;
        this.revisions = revisions;
        this.indexJobs = indexJobs;
        this.embeddings = embeddings;
    }

    /**
     * 导入指定目录下旧版 memory/*.md 文件。fingerprint 唯一约束使重复执行保持幂等。
     */
    @Transactional
    public LegacyMemoryImportResult importFrom(Path sourceRoot) {
        if (sourceRoot == null || !Files.isDirectory(sourceRoot)) {
            log.warn("Legacy SWE-bench memory root is unavailable: {}", sourceRoot);
            return new LegacyMemoryImportResult(0, 0, 0, 0);
        }

        List<Path> files = findLegacyFiles(sourceRoot);
        int imported = 0;
        int existing = 0;
        int skipped = 0;
        for (Path file : files) {
            Optional<ParsedLegacyMemory> parsed = parse(file);
            if (parsed.isEmpty()) {
                skipped++;
                continue;
            }

            MemoryContent content = parsed.get().content();
            String fingerprint = MemoryFingerprint.from(content);
            if (memories.findByScopeKeyAndFingerprint(HISTORICAL_SWE_BENCH_SCOPE, fingerprint).isPresent()) {
                existing++;
                continue;
            }

            SemanticMemory memory = memories.saveAndFlush(new SemanticMemory(
                    MemoryScopeType.GLOBAL, null, HISTORICAL_SWE_BENCH_SCOPE, fingerprint, content
            ));
            revisions.save(new MemoryRevision(
                    memory,
                    null,
                    "Imported verified SWE-bench Lite legacy memory from " + file.getFileName()
            ));
            enqueueIndex(memory);
            imported++;
        }
        return new LegacyMemoryImportResult(files.size(), imported, existing, skipped);
    }

    private List<Path> findLegacyFiles(Path sourceRoot) {
        try (Stream<Path> paths = Files.walk(sourceRoot, MAX_SCAN_DEPTH)) {
            return paths.filter(Files::isRegularFile)
                    .filter(this::isLegacyMemoryFile)
                    .sorted(Comparator.comparing(Path::toString))
                    .toList();
        } catch (IOException error) {
            throw new IllegalStateException("Unable to scan legacy memory root: " + sourceRoot, error);
        }
    }

    private boolean isLegacyMemoryFile(Path path) {
        Path parent = path.getParent();
        if (parent == null || parent.getFileName() == null || !"memory".equals(parent.getFileName().toString())) {
            return false;
        }
        String name = path.getFileName().toString();
        return name.endsWith(".md") && LEGACY_PREFIXES.stream().anyMatch(name::startsWith);
    }

    private Optional<ParsedLegacyMemory> parse(Path file) {
        final String raw;
        try {
            raw = Files.readString(file, StandardCharsets.UTF_8).replace("\r\n", "\n");
        } catch (IOException error) {
            log.warn("Skipping unreadable legacy memory {}", file, error);
            return Optional.empty();
        }

        FrontMatter frontMatter = splitFrontMatter(raw);
        String type = firstNonBlank(frontMatter.metadata().get("type"), typeFromName(file.getFileName().toString()));
        String description = firstNonBlank(
                frontMatter.metadata().get("description"), humanize(file.getFileName().toString())
        );
        Map<String, String> sections = parseSections(frontMatter.body());
        String problem = firstNonBlank(
                sections.get("problemsignatures"), sections.get("problemsignature"), sections.get("inputevidence")
        );
        String applicability = firstNonBlank(sections.get("applicability"), problem, description);
        String strategy = firstNonBlank(
                sections.get("repairstrategy"), sections.get("successfuloutput"), frontMatter.body().strip(), description
        );
        String verification = firstNonBlank(sections.get("verificationcontract"), sections.get("verificationstrategy"),
                sections.get("validation"));
        String antiPatterns = joinNonBlank(sections.get("failedattempt"), sections.get("feedback"));
        String framework = frameworkFor(description + "\n" + frontMatter.body());
        String summary = joinNonBlank(description, problem);
        String retrievalText = joinNonBlank(
                description,
                problem.isBlank() ? "" : "Problem signature: " + problem,
                applicability.isBlank() ? "" : "Applicability: " + applicability
        );
        if (summary.isBlank() || applicability.isBlank() || strategy.isBlank()) {
            log.warn("Skipping incomplete legacy memory {}", file);
            return Optional.empty();
        }

        return Optional.of(new ParsedLegacyMemory(type, new MemoryContent(
                truncate(description, 255),
                truncate(summary, 8_000),
                truncate(retrievalText, 2_000),
                truncate(applicability, 8_000),
                truncate(strategy, 12_000),
                blankToNull(truncate(verification, 8_000)),
                blankToNull(truncate(antiPatterns, 8_000)),
                "Python",
                framework,
                "legacy-" + type.replace('_', '-'),
                null
        )));
    }

    private Map<String, String> parseSections(String body) {
        Map<String, StringBuilder> values = new LinkedHashMap<>();
        StringBuilder unsectioned = new StringBuilder();
        String current = null;
        for (String line : body.split("\n", -1)) {
            String heading = headingKey(line);
            if (heading != null) {
                current = heading;
                values.computeIfAbsent(current, ignored -> new StringBuilder());
                continue;
            }
            int delimiter = line.indexOf(':');
            String inlineKey = delimiter > 0 ? normalizeKey(line.substring(0, delimiter)) : "";
            if (INLINE_FIELDS.contains(inlineKey)) {
                current = normalizeKey(inlineKey);
                StringBuilder value = values.computeIfAbsent(current, ignored -> new StringBuilder());
                appendLine(value, line.substring(delimiter + 1).strip());
                continue;
            }
            if (current == null) appendLine(unsectioned, line);
            else appendLine(values.computeIfAbsent(current, ignored -> new StringBuilder()), line);
        }
        if (!unsectioned.toString().isBlank()) values.putIfAbsent("body", unsectioned);

        Map<String, String> result = new LinkedHashMap<>();
        values.forEach((key, value) -> result.put(key, value.toString().strip()));
        return result;
    }

    private FrontMatter splitFrontMatter(String raw) {
        if (!raw.startsWith("---\n")) return new FrontMatter(Map.of(), raw);
        int end = raw.indexOf("\n---\n", 4);
        if (end < 0) return new FrontMatter(Map.of(), raw);
        Map<String, String> metadata = new LinkedHashMap<>();
        for (String line : raw.substring(4, end).split("\n")) {
            int delimiter = line.indexOf(':');
            if (delimiter <= 0) continue;
            String key = line.substring(0, delimiter).strip().toLowerCase(Locale.ROOT);
            String value = unquote(line.substring(delimiter + 1).strip());
            metadata.put(key, value);
        }
        return new FrontMatter(metadata, raw.substring(end + 5));
    }

    private void enqueueIndex(SemanticMemory memory) {
        if (!embeddings.isEnabled()) return;
        if (indexJobs.findByMemoryIdAndMemoryRevisionAndEmbeddingProfile(
                memory.getId(), memory.getVersion(), embeddings.profile()
        ).isEmpty()) {
            indexJobs.save(new MemoryIndexJob(memory.getId(), memory.getVersion(), embeddings.profile()));
        }
    }

    private static String headingKey(String line) {
        String trimmed = line.strip();
        if (!trimmed.startsWith("#")) return null;
        int offset = 0;
        while (offset < trimmed.length() && trimmed.charAt(offset) == '#') offset++;
        if (offset == 0 || offset >= trimmed.length() || !Character.isWhitespace(trimmed.charAt(offset))) return null;
        return normalizeKey(trimmed.substring(offset).strip());
    }

    private static String typeFromName(String fileName) {
        for (String prefix : LEGACY_PREFIXES) {
            if (fileName.startsWith(prefix)) return prefix.substring(0, prefix.length() - 1);
        }
        return "legacy_memory";
    }

    private static String humanize(String fileName) {
        return fileName.replaceFirst("\\.md$", "").replaceFirst("^(repair_recipe_|repair_episode_|semantic_insight_)", "")
                .replace('_', ' ').strip();
    }

    private static String frameworkFor(String value) {
        String lower = value.toLowerCase(Locale.ROOT);
        if (lower.contains("django")) return "Django";
        if (lower.contains("sympy")) return "SymPy";
        return null;
    }

    private static String unquote(String value) {
        if (value.length() >= 2) {
            char first = value.charAt(0);
            char last = value.charAt(value.length() - 1);
            if ((first == '\'' && last == '\'') || (first == '"' && last == '"')) {
                return value.substring(1, value.length() - 1);
            }
        }
        return value;
    }

    private static String normalizeKey(String value) {
        return value.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9]+", "");
    }

    private static void appendLine(StringBuilder target, String line) {
        if (target.length() > 0) target.append('\n');
        target.append(line);
    }

    private static String firstNonBlank(String... values) {
        for (String value : values) {
            if (value != null && !value.isBlank()) return value.strip();
        }
        return "";
    }

    private static String joinNonBlank(String... values) {
        List<String> nonBlank = new ArrayList<>();
        for (String value : values) {
            if (value != null && !value.isBlank()) nonBlank.add(value.strip());
        }
        return String.join("\n\n", new LinkedHashSet<>(nonBlank));
    }

    private static String blankToNull(String value) {
        return value == null || value.isBlank() ? null : value;
    }

    private static String truncate(String value, int limit) {
        if (value == null || value.length() <= limit) return value == null ? "" : value;
        return value.substring(0, limit);
    }

    private record FrontMatter(Map<String, String> metadata, String body) {
    }

    private record ParsedLegacyMemory(String type, MemoryContent content) {
    }
}
