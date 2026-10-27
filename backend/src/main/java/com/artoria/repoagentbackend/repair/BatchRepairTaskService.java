package com.artoria.repoagentbackend.repair;

import com.artoria.repoagentbackend.issue.RepositoryIssue;
import com.artoria.repoagentbackend.issue.RepositoryIssueService;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.UUID;

@Service
public class BatchRepairTaskService {
    private final RepositoryIssueService issues;
    private final RepairTaskService tasks;

    public BatchRepairTaskService(RepositoryIssueService issues, RepairTaskService tasks) {
        this.issues = issues;
        this.tasks = tasks;
    }

    public BatchRepairTaskResponse create(
            UUID actorId, String batchKey, BatchCreateRepairTasksRequest request
    ) {
        List<UUID> issueIds = new ArrayList<>(new LinkedHashSet<>(request.issueIds()));
        List<BatchRepairTaskItemResponse> results = new ArrayList<>();
        for (UUID issueId : issueIds) {
            try {
                RepositoryIssue issue = issues.getRequired(issueId);
                if (issue.getBaseSha() == null || issue.getBaseSha().isBlank()) {
                    throw new IllegalArgumentException("Issue does not have a repair baseline SHA");
                }
                RepairTaskResponse task = tasks.createRepairable(
                        issueId,
                        actorId,
                        new CreateRepairTaskRequest(issue.getBaseSha()),
                        batchKey + ":" + issueId,
                        request.runKey()
                );
                results.add(BatchRepairTaskItemResponse.accepted(issueId, task));
            } catch (RuntimeException error) {
                String message = error.getMessage() == null
                        ? error.getClass().getSimpleName() : error.getMessage();
                results.add(BatchRepairTaskItemResponse.rejected(issueId, message));
            }
        }
        int accepted = (int) results.stream().filter(BatchRepairTaskItemResponse::accepted).count();
        return new BatchRepairTaskResponse(
                issueIds.size(), accepted, issueIds.size() - accepted, List.copyOf(results)
        );
    }
}
