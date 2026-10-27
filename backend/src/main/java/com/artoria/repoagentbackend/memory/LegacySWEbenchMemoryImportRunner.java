package com.artoria.repoagentbackend.memory;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.nio.file.InvalidPathException;
import java.nio.file.Path;

/** 仅在显式开启配置时执行的历史记忆导入入口。 */
@Component
@ConditionalOnProperty(name = "app.memory.legacy-import.enabled", havingValue = "true")
public class LegacySWEbenchMemoryImportRunner implements ApplicationRunner {
    private static final Logger log = LoggerFactory.getLogger(LegacySWEbenchMemoryImportRunner.class);

    private final LegacySWEbenchMemoryImportService importer;
    private final String sourceRoot;

    public LegacySWEbenchMemoryImportRunner(
            LegacySWEbenchMemoryImportService importer,
            @Value("${app.memory.legacy-import.source-root:}") String sourceRoot
    ) {
        this.importer = importer;
        this.sourceRoot = sourceRoot;
    }

    @Override
    public void run(ApplicationArguments args) {
        if (sourceRoot == null || sourceRoot.isBlank()) {
            log.warn("Legacy SWE-bench memory import is enabled but no source root was configured");
            return;
        }
        try {
            LegacyMemoryImportResult result = importer.importFrom(Path.of(sourceRoot));
            log.info("Legacy SWE-bench memory import finished: scanned={}, imported={}, existing={}, skipped={}",
                    result.scanned(), result.imported(), result.existing(), result.skipped());
        } catch (InvalidPathException error) {
            log.warn("Invalid legacy SWE-bench memory root: {}", sourceRoot, error);
        }
    }
}
