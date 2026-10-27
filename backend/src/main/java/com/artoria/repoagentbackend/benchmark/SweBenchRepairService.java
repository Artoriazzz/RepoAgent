package com.artoria.repoagentbackend.benchmark;

import com.artoria.repoagentbackend.issue.RepositoryIssue;
import com.artoria.repoagentbackend.issue.RepositoryIssueService;
import com.artoria.repoagentbackend.repair.CreateRepairTaskRequest;
import com.artoria.repoagentbackend.repair.RepairTaskResponse;
import com.artoria.repoagentbackend.repair.RepairTaskService;
import com.artoria.repoagentbackend.repository.CodeRepository;
import com.artoria.repoagentbackend.repository.CodeRepositoryService;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.UUID;

@Service
public class SweBenchRepairService {
    private final CodeRepositoryService repositories;
    private final RepositoryIssueService issues;
    private final RepairTaskService tasks;

    public SweBenchRepairService(
            CodeRepositoryService repositories,
            RepositoryIssueService issues,
            RepairTaskService tasks
    ) {
        this.repositories = repositories;
        this.issues = issues;
        this.tasks = tasks;
    }

    @Transactional
    public SweBenchRepairResponse submit(
            UUID actorId,
            String idempotencyKey,
            CreateSweBenchRepairRequest request
    ) {
        CodeRepository repository = repositories.findOrCreateSweBench(actorId, request.repository());
        RepositoryIssue issue = issues.findOrCreateSweBench(
                repository,
                request.instanceId(),
                "SWE-bench: " + request.instanceId(),
                request.problemStatement(),
                request.datasetSplit(),
                request.baseCommit()
        );
        RepairTaskResponse task = tasks.create(
                issue.getId(),
                actorId,
                new CreateRepairTaskRequest(request.baseCommit()),
                idempotencyKey,
                request.runKey()
        );
        return new SweBenchRepairResponse(
                request.instanceId(),
                request.repository(),
                request.datasetSplit(),
                repository.getId(),
                issue.getId(),
                task
        );
    }

    @Transactional
    public SweBenchIssueImportResponse importIssue(
            UUID actorId,
            CreateSweBenchRepairRequest request
    ) {
        CodeRepository repository = repositories.findOrCreateSweBench(actorId, request.repository());
        RepositoryIssue issue = issues.findOrCreateSweBench(
                repository,
                request.instanceId(),
                "SWE-bench: " + request.instanceId(),
                request.problemStatement(),
                request.datasetSplit(),
                request.baseCommit()
        );
        return new SweBenchIssueImportResponse(
                request.instanceId(), request.repository(), request.datasetSplit(),
                repository.getId(), issue.getId()
        );
    }
}
