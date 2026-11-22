package com.artoria.repoagentbackend.repair;

import com.artoria.repoagentbackend.common.ForbiddenException;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.RequestParam;

import java.util.UUID;

@RestController
@RequestMapping("/internal/repair-tasks")
public class RepairTaskWorkerController {
    private final RepairWorkerTokenVerifier tokenVerifier;
    private final RepairTaskService taskService;

    public RepairTaskWorkerController(RepairWorkerTokenVerifier tokenVerifier, RepairTaskService taskService) {
        this.tokenVerifier = tokenVerifier;
        this.taskService = taskService;
    }

    @PostMapping("/{taskId}/updates")
    public RepairTaskResponse update(
            @PathVariable UUID taskId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody RepairTaskWorkerUpdateRequest request
    ) {
        verifyToken(token);
        return taskService.updateFromWorker(taskId, request);
    }

    @PostMapping("/{taskId}/claim")
    public RepairTaskControlResponse claim(
            @PathVariable UUID taskId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody RepairTaskClaimRequest request
    ) {
        verifyToken(token);
        return taskService.loadClaimedContexts(taskService.claim(taskId, request));
    }

    @PostMapping("/{taskId}/stage-handoffs")
    public RepairTaskControlResponse handoffStage(
            @PathVariable UUID taskId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody RepairTaskStageHandoffRequest request
    ) {
        verifyToken(token);
        return taskService.handoffStage(taskId, request);
    }

    @PostMapping("/{taskId}/pipeline-completions")
    public RepairTaskResponse completePipeline(
            @PathVariable UUID taskId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody RepairTaskPipelineCompletionRequest request
    ) {
        verifyToken(token);
        return taskService.completePipeline(taskId, request);
    }

    @PostMapping("/{taskId}/lease/renew")
    public RepairTaskControlResponse renewLease(
            @PathVariable UUID taskId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody RepairTaskLeaseRequest request
    ) {
        verifyToken(token);
        return taskService.renewLease(taskId, request);
    }

    @GetMapping("/{taskId}/control")
    public RepairTaskControlResponse control(
            @PathVariable UUID taskId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @RequestParam String workerId,
            @RequestParam UUID leaseToken,
            @RequestParam(required = false) Long stageSequence,
            @RequestParam(required = false) Long leaseEpoch
    ) {
        verifyToken(token);
        return taskService.control(taskId, workerId, leaseToken, stageSequence, leaseEpoch);
    }

    @PostMapping("/{taskId}/events")
    public RepairTaskEventResponse event(
            @PathVariable UUID taskId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody RepairTaskProgressRequest request
    ) {
        verifyToken(token);
        return taskService.reportProgress(taskId, request);
    }

    @GetMapping("/{taskId}/context")
    public RepairTaskWorkerContextResponse context(
            @PathVariable UUID taskId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token
    ) {
        verifyToken(token);
        return taskService.getWorkerContext(taskId);
    }

    @PostMapping("/{taskId}/evaluation")
    public RepairTaskResponse evaluation(
            @PathVariable UUID taskId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody BenchmarkEvaluationUpdateRequest request
    ) {
        verifyToken(token);
        return taskService.updateEvaluation(taskId, request);
    }

    private void verifyToken(String token) {
        if (!tokenVerifier.isValid(token)) {
            throw new ForbiddenException("Invalid RepoAgent worker token");
        }
    }
}
