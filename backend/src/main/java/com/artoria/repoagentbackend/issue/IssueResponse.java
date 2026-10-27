package com.artoria.repoagentbackend.issue;

import java.util.UUID;

public record IssueResponse(
        UUID id,
        UUID repositoryId,
        Integer githubIssueNumber,
        String externalId,
        String benchmarkDatasetSplit,
        String baseSha,
        String title,
        String body,
        IssueState state
) {
    static IssueResponse from(RepositoryIssue issue) {
        return new IssueResponse(
                issue.getId(),
                issue.getRepository().getId(),
                issue.getGithubIssueNumber(),
                issue.getExternalId(),
                issue.getBenchmarkDatasetSplit(),
                issue.getBaseSha(),
                issue.getTitle(),
                issue.getBody(),
                issue.getState()
        );
    }
}
