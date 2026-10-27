package com.artoria.repoagentbackend.github;

import com.artoria.repoagentbackend.issue.IssueState;
import com.artoria.repoagentbackend.issue.RepositoryIssueService;
import com.artoria.repoagentbackend.repository.CodeRepository;
import com.artoria.repoagentbackend.repository.CodeRepositoryJpaRepository;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class GitHubWebhookServiceTest {

    @Mock
    private WebhookDeliveryStore deliveries;

    @Mock
    private CodeRepositoryJpaRepository repositories;

    @Mock
    private RepositoryIssueService issues;

    @Test
    void synchronizesTheClosedIssueStateForABoundRepository() {
        CodeRepository repository = new CodeRepository(42L, "octo", "demo", "main", null);
        when(deliveries.recordIfNew(eq("delivery-1"), eq("issues"), any(Instant.class))).thenReturn(true);
        when(repositories.findByGithubRepositoryId(42L)).thenReturn(Optional.of(repository));

        service().accept("delivery-1", "issues", """
                {"repository":{"id":42},"issue":{"number":7,"title":"Broken parser","body":"Steps","state":"closed"}}
                """.getBytes(StandardCharsets.UTF_8));

        ArgumentCaptor<IssueState> state = ArgumentCaptor.forClass(IssueState.class);
        verify(issues).synchronizeFromGitHub(eq(repository), eq(7), eq("Broken parser"), eq("Steps"), state.capture());
        assertThat(state.getValue()).isEqualTo(IssueState.CLOSED);
    }

    @Test
    void ignoresPullRequestsDeliveredAsIssuePayloads() {
        when(deliveries.recordIfNew(eq("delivery-2"), eq("issues"), any(Instant.class))).thenReturn(true);

        service().accept("delivery-2", "issues", """
                {"repository":{"id":42},"issue":{"number":7,"title":"A pull request","pull_request":{}}}
                """.getBytes(StandardCharsets.UTF_8));

        verify(repositories, never()).findByGithubRepositoryId(any(Long.class));
        verify(issues, never()).synchronizeFromGitHub(any(), any(Integer.class), any(), any(), any());
    }

    private GitHubWebhookService service() {
        return new GitHubWebhookService(deliveries, repositories, issues, new ObjectMapper());
    }
}
