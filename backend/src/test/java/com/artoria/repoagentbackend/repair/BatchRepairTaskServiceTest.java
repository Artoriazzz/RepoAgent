package com.artoria.repoagentbackend.repair;

import com.artoria.repoagentbackend.issue.RepositoryIssue;
import com.artoria.repoagentbackend.issue.RepositoryIssueService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class BatchRepairTaskServiceTest {
    @Mock
    private RepositoryIssueService issues;
    @Mock
    private RepairTaskService tasks;

    @Test
    void createsRepairableItemsAndReportsPerItemFailures() {
        UUID actorId = UUID.randomUUID();
        UUID repairableId = UUID.randomUUID();
        UUID missingBaselineId = UUID.randomUUID();
        RepositoryIssue repairable = mock(RepositoryIssue.class);
        RepositoryIssue missingBaseline = mock(RepositoryIssue.class);
        RepairTaskResponse created = mock(RepairTaskResponse.class);
        when(repairable.getBaseSha()).thenReturn("abcdef1");
        when(missingBaseline.getBaseSha()).thenReturn(null);
        when(issues.getRequired(repairableId)).thenReturn(repairable);
        when(issues.getRequired(missingBaselineId)).thenReturn(missingBaseline);
        when(tasks.createRepairable(
                eq(repairableId), eq(actorId), any(), eq("batch-1:" + repairableId), eq("demo-run")
        )).thenReturn(created);

        BatchRepairTaskResponse response = service().create(
                actorId,
                "batch-1",
                new BatchCreateRepairTasksRequest(
                        List.of(repairableId, repairableId, missingBaselineId), "demo-run"
                )
        );

        assertThat(response.requested()).isEqualTo(2);
        assertThat(response.accepted()).isEqualTo(1);
        assertThat(response.rejected()).isEqualTo(1);
        assertThat(response.items()).extracting(BatchRepairTaskItemResponse::issueId)
                .containsExactly(repairableId, missingBaselineId);
        assertThat(response.items().get(1).error()).contains("baseline SHA");
        verify(tasks).createRepairable(
                eq(repairableId), eq(actorId), any(), eq("batch-1:" + repairableId), eq("demo-run")
        );
        verify(tasks, never()).createRepairable(
                eq(missingBaselineId), eq(actorId), any(), any(), any()
        );
    }

    private BatchRepairTaskService service() {
        return new BatchRepairTaskService(issues, tasks);
    }
}
