package com.artoria.repoagentbackend.repair;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.validation.annotation.Validated;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.web.PageableDefault;
import org.springframework.data.domain.Sort;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.time.Instant;
import java.util.UUID;

@RestController
@Validated
@RequestMapping("/api")
public class RepairTaskController {
    private final RepairTaskService taskService;
    private final BatchRepairTaskService batchTaskService;

    public RepairTaskController(RepairTaskService taskService, BatchRepairTaskService batchTaskService) {
        this.taskService = taskService;
        this.batchTaskService = batchTaskService;
    }

    @PostMapping("/issues/{issueId}/repair-tasks")
    @ResponseStatus(HttpStatus.ACCEPTED)
    public RepairTaskResponse create(
            @PathVariable UUID issueId,
            @RequestHeader("X-User-Id") UUID actorId,
            @RequestHeader("Idempotency-Key") @NotBlank String idempotencyKey,
            @Valid @RequestBody CreateRepairTaskRequest request
    ) {
        return taskService.create(issueId, actorId, request, idempotencyKey);
    }

    @PostMapping("/repair-tasks/batch")
    @ResponseStatus(HttpStatus.ACCEPTED)
    public BatchRepairTaskResponse createBatch(
            @RequestHeader("X-User-Id") UUID actorId,
            @RequestHeader("Idempotency-Key") @NotBlank String idempotencyKey,
            @Valid @RequestBody BatchCreateRepairTasksRequest request
    ) {
        return batchTaskService.create(actorId, idempotencyKey, request);
    }

    @GetMapping("/repair-tasks/{taskId}")
    public RepairTaskResponse get(
            @PathVariable UUID taskId,
            @RequestHeader("X-User-Id") UUID actorId
    ) {
        return taskService.get(taskId, actorId);
    }

    @GetMapping("/repair-tasks")
    public Page<RepairTaskResponse> list(
            @RequestHeader("X-User-Id") UUID actorId,
            @RequestParam(required = false) UUID repositoryId,
            @RequestParam(required = false) RepairTaskStatus status,
            @RequestParam(required = false) String runKey,
            @PageableDefault(size = 20, sort = "createdAt", direction = Sort.Direction.DESC) Pageable pageable
    ) {
        return taskService.list(actorId, repositoryId, status, runKey, pageable);
    }

    @GetMapping("/repair-tasks/{taskId}/events")
    public Page<RepairTaskEventResponse> events(
            @PathVariable UUID taskId,
            @RequestHeader("X-User-Id") UUID actorId,
            @PageableDefault(size = 100) Pageable pageable
    ) {
        return taskService.events(taskId, actorId, pageable);
    }

    @GetMapping("/repair-tasks/{taskId}/progress")
    public RepairTaskProgressResponse progress(
            @PathVariable UUID taskId,
            @RequestHeader("X-User-Id") UUID actorId
    ) {
        return taskService.progress(taskId, actorId);
    }

    @GetMapping(value = "/repair-tasks/{taskId}/events/stream", produces = "text/event-stream")
    public SseEmitter streamEvents(
            @PathVariable UUID taskId,
            @RequestHeader("X-User-Id") UUID actorId,
            @RequestParam(required = false)
            @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) Instant after
    ) {
        return taskService.streamEvents(taskId, actorId, after);
    }

    @GetMapping("/repair-tasks/statistics")
    public RepairTaskStatisticsResponse statistics(
            @RequestHeader("X-User-Id") UUID actorId,
            @RequestParam(required = false) UUID repositoryId,
            @RequestParam(required = false) String runKey
    ) {
        return taskService.statistics(actorId, repositoryId, runKey);
    }

    @PostMapping("/repair-tasks/{taskId}/cancel")
    public RepairTaskResponse cancel(
            @PathVariable UUID taskId,
            @RequestHeader("X-User-Id") UUID actorId
    ) {
        return taskService.cancel(taskId, actorId);
    }

    @PostMapping("/repair-tasks/{taskId}/retry")
    @ResponseStatus(HttpStatus.ACCEPTED)
    public RepairTaskResponse retry(
            @PathVariable UUID taskId,
            @RequestHeader("X-User-Id") UUID actorId,
            @RequestHeader("Idempotency-Key") @NotBlank String idempotencyKey
    ) {
        return taskService.retry(taskId, actorId, idempotencyKey);
    }

    @DeleteMapping("/repair-tasks/{taskId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void delete(
            @PathVariable UUID taskId,
            @RequestHeader("X-User-Id") UUID actorId
    ) {
        taskService.delete(taskId, actorId);
    }

    @DeleteMapping("/repair-tasks/completed")
    public ClearRepairTasksResponse clearCompleted(
            @RequestHeader("X-User-Id") UUID actorId
    ) {
        return taskService.clearCompleted(actorId);
    }
}
