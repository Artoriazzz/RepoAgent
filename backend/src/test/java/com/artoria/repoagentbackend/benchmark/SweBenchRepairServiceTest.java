package com.artoria.repoagentbackend.benchmark;

import com.artoria.repoagentbackend.issue.RepositoryIssue;
import com.artoria.repoagentbackend.issue.RepositoryIssueService;
import com.artoria.repoagentbackend.repair.RepairTaskResponse;
import com.artoria.repoagentbackend.repair.RepairTaskService;
import com.artoria.repoagentbackend.repository.CodeRepository;
import com.artoria.repoagentbackend.repository.CodeRepositoryService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.mockito.Mockito.mock;

@ExtendWith(MockitoExtension.class)
class SweBenchRepairServiceTest {
    @Mock
    private CodeRepositoryService repositories;
    @Mock
    private RepositoryIssueService issues;
    @Mock
    private RepairTaskService tasks;

    @Test
    void mapsAnInstanceToRepositoryIssueAndRepairTask() {
        UUID actorId = UUID.randomUUID();
        UUID repositoryId = UUID.randomUUID();
        UUID issueId = UUID.randomUUID();
        CodeRepository repository = mock(CodeRepository.class);
        RepositoryIssue issue = mock(RepositoryIssue.class);
        when(repository.getId()).thenReturn(repositoryId);
        when(issue.getId()).thenReturn(issueId);
        RepairTaskResponse task = mock(RepairTaskResponse.class);
        when(repositories.findOrCreateSweBench(actorId, "pallets/flask")).thenReturn(repository);
        when(issues.findOrCreateSweBench(
                eq(repository), eq("pallets__flask-4045"), any(), eq("Fix the parser."), eq("lite"),
                eq("abcdef1")
        )).thenReturn(issue);
        when(tasks.create(eq(issueId), eq(actorId), any(), eq("run-1"), eq("pass-1"))).thenReturn(task);

        SweBenchRepairResponse response = service().submit(actorId, "run-1", new CreateSweBenchRepairRequest(
                "pallets__flask-4045", "pallets/flask", "abcdef1", "Fix the parser.", "lite", "pass-1"
        ));

        assertThat(response.instanceId()).isEqualTo("pallets__flask-4045");
        assertThat(response.repositoryId()).isEqualTo(repositoryId);
        assertThat(response.issueId()).isEqualTo(issueId);
        assertThat(response.task()).isSameAs(task);
        verify(tasks).create(eq(issueId), eq(actorId), any(), eq("run-1"), eq("pass-1"));
    }

    private SweBenchRepairService service() {
        return new SweBenchRepairService(repositories, issues, tasks);
    }
}
