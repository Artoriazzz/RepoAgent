package com.artoria.repoagentbackend.repair;

import com.artoria.repoagentbackend.issue.RepositoryIssue;
import com.artoria.repoagentbackend.repository.CodeRepository;
import com.artoria.repoagentbackend.repository.RepositoryProvider;

import java.util.UUID;

public record RepairTaskWorkerContextResponse(
        UUID taskId,
        UUID issueId,
        UUID repositoryId,
        RepositoryProvider repositoryProvider,
        String repositoryExternalId,
        String mirrorKey,
        Long githubRepositoryId,
        String repositoryFullName,
        String cloneUrl,
        String defaultBranch,
        Integer githubIssueNumber,
        String issueExternalId,
        String benchmarkDatasetSplit,
        String issueTitle,
        String issueBody,
        String baseSha,
        String branchName,
        int attemptNumber,
        String previousFailureSummary,
        RepairPipelineStage pipelineStage
) {
    static RepairTaskWorkerContextResponse from(RepairTask task) {
        RepositoryIssue issue = task.getIssue();
        CodeRepository repository = issue.getRepository();
        String fullName = repository.getOwnerLogin() + "/" + repository.getName();
        return new RepairTaskWorkerContextResponse(
                task.getId(),
                issue.getId(),
                repository.getId(),
                repository.getProvider(),
                repository.getExternalId(),
                repository.getMirrorKey(),
                repository.getGithubRepositoryId(),
                fullName,
                repository.getProvider() == RepositoryProvider.GITHUB
                        ? "https://github.com/" + fullName + ".git"
                        : null,
                repository.getDefaultBranch(),
                issue.getGithubIssueNumber(),
                issue.getExternalId(),
                issue.getBenchmarkDatasetSplit(),
                issue.getTitle(),
                issue.getBody(),
                task.getBaseSha(),
                task.getBranchName(),
                task.getAttemptNumber(),
                task.getPreviousFailureSummary(),
                task.getPipelineStage()
        );
    }
}
