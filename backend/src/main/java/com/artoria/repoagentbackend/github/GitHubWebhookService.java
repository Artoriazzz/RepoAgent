package com.artoria.repoagentbackend.github;

import com.artoria.repoagentbackend.common.BadRequestException;
import com.artoria.repoagentbackend.issue.IssueState;
import com.artoria.repoagentbackend.issue.RepositoryIssueService;
import com.artoria.repoagentbackend.repository.CodeRepositoryJpaRepository;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.io.IOException;
import java.time.Instant;

@Service
public class GitHubWebhookService {
    private final WebhookDeliveryStore deliveries;
    private final CodeRepositoryJpaRepository repositories;
    private final RepositoryIssueService issueService;
    private final ObjectMapper objectMapper;

    public GitHubWebhookService(
            WebhookDeliveryStore deliveries,
            CodeRepositoryJpaRepository repositories,
            RepositoryIssueService issueService,
            ObjectMapper objectMapper
    ) {
        this.deliveries = deliveries;
        this.repositories = repositories;
        this.issueService = issueService;
        this.objectMapper = objectMapper;
    }

    @Transactional
    public void accept(String deliveryId, String eventType, byte[] payload) {
        if (!deliveries.recordIfNew(deliveryId, eventType, Instant.now())) {
            return;
        }
        if (!"issues".equals(eventType)) {
            return;
        }
        synchronizeIssue(payload);
    }

    private void synchronizeIssue(byte[] payload) {
        try {
            JsonNode root = objectMapper.readTree(payload);
            long githubRepositoryId = root.path("repository").path("id").asLong(-1);
            JsonNode issue = root.path("issue");
            if (issue.hasNonNull("pull_request")) {
                return;
            }
            int issueNumber = issue.path("number").asInt(-1);
            String title = issue.path("title").asText("").trim();
            if (githubRepositoryId < 0 || issueNumber < 1 || title.isEmpty()) {
                throw new BadRequestException("Webhook is missing a valid repository or issue payload");
            }
            repositories.findByGithubRepositoryId(githubRepositoryId).ifPresent(repository ->
                    issueService.synchronizeFromGitHub(
                            repository,
                            issueNumber,
                            title,
                            issue.path("body").asText(""),
                            "closed".equalsIgnoreCase(issue.path("state").asText())
                                    ? IssueState.CLOSED
                                    : IssueState.OPEN
                    )
            );
        } catch (IOException exception) {
            throw new BadRequestException("Webhook payload is not valid JSON");
        }
    }
}
