package com.artoria.repoagentbackend.benchmark;

import java.util.UUID;

public record SweBenchIssueImportResponse(
        String instanceId,
        String repository,
        String datasetSplit,
        UUID repositoryId,
        UUID issueId
) {
}
