package com.artoria.repoagentbackend.repair;

import com.artoria.repoagentbackend.common.NotFoundException;
import com.artoria.repoagentbackend.common.BadRequestException;
import com.artoria.repoagentbackend.common.ConflictException;
import com.artoria.repoagentbackend.issue.RepositoryIssue;
import com.artoria.repoagentbackend.issue.RepositoryIssueService;
import com.artoria.repoagentbackend.repository.RepositoryAccessService;
import com.artoria.repoagentbackend.repository.RepositoryProvider;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.PageRequest;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.time.Instant;
import java.util.EnumMap;
import java.util.EnumSet;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Service
public class RepairTaskService {
    private static final EnumSet<RepairTaskStatus> ACTIVE_STATUSES = EnumSet.of(
            RepairTaskStatus.CREATED,
            RepairTaskStatus.QUEUED,
            RepairTaskStatus.ANALYZING,
            RepairTaskStatus.FIXING,
            RepairTaskStatus.VALIDATING
    );
    private static final EnumSet<BenchmarkEvaluationStatus> BLOCKING_EVALUATION_STATUSES = EnumSet.of(
            BenchmarkEvaluationStatus.RUNNING,
            BenchmarkEvaluationStatus.RESOLVED
    );

    private final RepairTaskJpaRepository tasks;
    private final RepositoryIssueService issueService;
    private final RepairBranchNameFactory branchNameFactory;
    private final RepairOutboxService outbox;
    private final RepositoryAccessService accessService;
    private final RepairTaskEventJpaRepository taskEvents;
    private final WorkerNodeJpaRepository workers;
    private final WorkerPresenceStore workerPresence;
    private final RepairTaskEventStream eventStream;
    private final RepairAdmissionService admission;
    private final long eventStreamTimeoutMillis;

    public RepairTaskService(
            RepairTaskJpaRepository tasks,
            RepositoryIssueService issueService,
            RepairBranchNameFactory branchNameFactory,
            RepairOutboxService outbox,
            RepositoryAccessService accessService,
            RepairTaskEventJpaRepository taskEvents,
            WorkerNodeJpaRepository workers,
            WorkerPresenceStore workerPresence,
            RepairTaskEventStream eventStream,
            RepairAdmissionService admission,
            @org.springframework.beans.factory.annotation.Value(
                    "${app.repair.event-stream-timeout-millis:1800000}"
            ) long eventStreamTimeoutMillis
    ) {
        this.tasks = tasks;
        this.issueService = issueService;
        this.branchNameFactory = branchNameFactory;
        this.outbox = outbox;
        this.accessService = accessService;
        this.taskEvents = taskEvents;
        this.workers = workers;
        this.workerPresence = workerPresence;
        this.eventStream = eventStream;
        this.admission = admission;
        this.eventStreamTimeoutMillis = eventStreamTimeoutMillis;
    }

    @Transactional
    public RepairTaskResponse create(UUID issueId, UUID actorId, CreateRepairTaskRequest request, String idempotencyKey) {
        return create(issueId, actorId, request, idempotencyKey, null);
    }

    @Transactional
    public RepairTaskResponse create(
            UUID issueId,
            UUID actorId,
            CreateRepairTaskRequest request,
            String idempotencyKey,
            String runKey
    ) {
        // 同一用户重放同一请求必须直接返回原任务，不能再次占用额度或全局容量。
        var replay = tasks.findBySubmittedByUserIdAndIdempotencyKey(actorId, idempotencyKey);
        if (replay.isPresent()) return RepairTaskResponse.from(replay.get());
        admission.checkRequestRate(actorId);
        RepositoryIssue issue = issueService.getRequiredForUpdate(issueId);
        accessService.requireOwner(issue.getRepository().getId(), actorId);
        return tasks.findByIssueIdAndIdempotencyKey(issueId, idempotencyKey)
                .map(RepairTaskResponse::from)
                .orElseGet(() -> createNewTask(issue, actorId, request, idempotencyKey, runKey));
    }

    @Transactional
    public RepairTaskResponse createRepairable(
            UUID issueId,
            UUID actorId,
            CreateRepairTaskRequest request,
            String idempotencyKey,
            String runKey
    ) {
        var replay = tasks.findBySubmittedByUserIdAndIdempotencyKey(actorId, idempotencyKey);
        if (replay.isPresent()) return RepairTaskResponse.from(replay.get());
        admission.checkRequestRate(actorId);
        RepositoryIssue issue = issueService.getRequiredForUpdate(issueId);
        accessService.requireOwner(issue.getRepository().getId(), actorId);
        return tasks.findByIssueIdAndIdempotencyKey(issueId, idempotencyKey)
                .map(RepairTaskResponse::from)
                .orElseGet(() -> {
                    if (tasks.existsBlockingRepair(
                            issueId, ACTIVE_STATUSES, BLOCKING_EVALUATION_STATUSES
                    )) {
                        throw new ConflictException("Issue already has an active or resolved repair task");
                    }
                    return createNewTask(issue, actorId, request, idempotencyKey, runKey);
                });
    }

    private RepairTaskResponse createNewTask(
            RepositoryIssue issue,
            UUID actorId,
            CreateRepairTaskRequest request,
            String idempotencyKey,
            String runKey
    ) {
        UUID taskId = UUID.randomUUID();
        RepairTask task = new RepairTask(
                taskId,
                issue,
                request.baseSha(),
                issue.getExternalId() == null
                        ? branchNameFactory.create(issue.getGithubIssueNumber(), taskId)
                        : branchNameFactory.create(issue.getExternalId(), taskId),
                idempotencyKey
        );
        task.assignRunKey(runKey);
        admission.reserve(actorId, task);
        task.transitionTo(RepairTaskStatus.QUEUED);
        task = tasks.saveAndFlush(task);
        admission.recordInitialStage(task);
        appendEvent(task, RepairTaskEventType.TASK_CREATED, "QUEUED", "Repair task created", null);
        outbox.enqueue(task);
        return RepairTaskResponse.from(task);
    }

    @Transactional(readOnly = true)
    public RepairTaskResponse get(UUID taskId, UUID actorId) {
        RepairTask task = getRequired(taskId);
        accessService.requireMember(task.getIssue().getRepository().getId(), actorId);
        return RepairTaskResponse.from(task);
    }

    @Transactional(readOnly = true)
    public Page<RepairTaskResponse> list(
            UUID actorId,
            UUID repositoryId,
            RepairTaskStatus status,
            String runKey,
            Pageable pageable
    ) {
        return tasks.findAccessible(actorId, repositoryId, status, runKey, pageable).map(RepairTaskResponse::from);
    }

    @Transactional(readOnly = true)
    public Page<RepairTaskEventResponse> events(UUID taskId, UUID actorId, Pageable pageable) {
        RepairTask task = getRequired(taskId);
        accessService.requireMember(task.getIssue().getRepository().getId(), actorId);
        return taskEvents.findByTaskIdOrderByCreatedAtAsc(taskId, pageable).map(RepairTaskEventResponse::from);
    }

    @Transactional(readOnly = true)
    public RepairTaskProgressResponse progress(UUID taskId, UUID actorId) {
        RepairTask task = getRequired(taskId);
        accessService.requireMember(task.getIssue().getRepository().getId(), actorId);
        RepairTaskEventResponse latest = taskEvents.findFirstByTaskIdOrderByCreatedAtDesc(taskId)
                .map(RepairTaskEventResponse::from)
                .orElse(null);
        return new RepairTaskProgressResponse(
                task.getId(), task.getStatus(), task.isCancelRequested(), task.getWorkerId(),
                task.getLeaseExpiresAt(), task.getWorkflowStatus(), task.getCurrentPipelineStage(),
                task.getStageStatus(), task.getStageSequence(), latest
        );
    }

    @Transactional(readOnly = true)
    public SseEmitter streamEvents(UUID taskId, UUID actorId, Instant after) {
        RepairTask task = getRequired(taskId);
        accessService.requireMember(task.getIssue().getRepository().getId(), actorId);
        PageRequest replayLimit = PageRequest.of(0, 500);
        List<RepairTaskEventResponse> replay = (after == null
                ? taskEvents.findByTaskIdOrderByCreatedAtAsc(taskId, replayLimit)
                : taskEvents.findByTaskIdAndCreatedAtAfterOrderByCreatedAtAsc(taskId, after, replayLimit))
                .map(RepairTaskEventResponse::from)
                .getContent();
        return eventStream.open(taskId, replay, eventStreamTimeoutMillis);
    }

    @Transactional(readOnly = true)
    public RepairTaskStatisticsResponse statistics(UUID actorId, UUID repositoryId, String runKey) {
        List<RepairTask> accessible = tasks.findAccessibleForStatistics(actorId, repositoryId, runKey);
        Map<RepairTaskStatus, Long> byStatus = new EnumMap<>(RepairTaskStatus.class);
        Map<BenchmarkEvaluationStatus, Long> byEvaluation = new EnumMap<>(BenchmarkEvaluationStatus.class);
        long emptyPatchCount = 0;
        long durationMs = 0;
        long totalTokens = 0;
        for (RepairTask task : accessible) {
            byStatus.merge(task.getStatus(), 1L, Long::sum);
            byEvaluation.merge(task.getEvaluationStatus(), 1L, Long::sum);
            if ("EMPTY_PATCH".equals(task.getErrorCode())) emptyPatchCount++;
            durationMs += task.getDurationMs() == null ? 0 : task.getDurationMs();
            totalTokens += task.getTotalTokens() == null ? 0 : task.getTotalTokens();
        }
        return new RepairTaskStatisticsResponse(
                accessible.size(), byStatus, byEvaluation, emptyPatchCount, durationMs, totalTokens
        );
    }

    @Transactional
    public RepairTaskResponse cancel(UUID taskId, UUID actorId) {
        RepairTask task = getRequired(taskId);
        accessService.requireOwner(task.getIssue().getRepository().getId(), actorId);
        task.requestCancellation();
        appendEvent(task, RepairTaskEventType.CANCEL_REQUESTED, task.getStatus().name(), "Cancellation requested", null);
        if (task.getStatus() == RepairTaskStatus.QUEUED || task.getStatus() == RepairTaskStatus.PATCH_READY) {
            task.transitionTo(RepairTaskStatus.CANCELLED);
            task.recordCancellation("Task cancelled by repository owner");
            appendStatusEvent(task, "Task cancelled before active execution");
            admission.recordStageFailure(task, "CANCELLED", "Task cancelled by repository owner");
            admission.releaseTerminalCapacity(task);
        }
        return RepairTaskResponse.from(task);
    }

    @Transactional
    public void delete(UUID taskId, UUID actorId) {
        RepairTask task = getRequiredForUpdate(taskId);
        accessService.requireOwner(task.getIssue().getRepository().getId(), actorId);
        if (task.getResultStatus() == RepairResultStatus.PENDING) {
            throw new ConflictException("An active repair task must be cancelled before deletion");
        }
        task.markDeleted();
    }

    @Transactional
    public ClearRepairTasksResponse clearCompleted(UUID actorId) {
        List<RepairTask> accessible = tasks.findAccessibleForStatistics(actorId, null, null);
        int deleted = 0;
        int skippedActive = 0;
        for (RepairTask task : accessible) {
            if (task.getResultStatus() == RepairResultStatus.PENDING) {
                skippedActive++;
                continue;
            }
            accessService.requireOwner(task.getIssue().getRepository().getId(), actorId);
            task.markDeleted();
            deleted++;
        }
        return new ClearRepairTasksResponse(deleted, skippedActive);
    }

    @Transactional
    public RepairTaskResponse retry(UUID taskId, UUID actorId, String idempotencyKey) {
        RepairTask original = getRequiredForUpdate(taskId);
        issueService.getRequiredForUpdate(original.getIssue().getId());
        accessService.requireOwner(original.getIssue().getRepository().getId(), actorId);
        if (!isRetrySource(original)) {
            throw new ConflictException("Only completed, failed, or cancelled tasks can be retried");
        }
        if (original.getAttemptNumber() >= 2) {
            throw new ConflictException("Mode 3 allows only one retry after the initial repair");
        }
        return tasks.findByIssueIdAndIdempotencyKey(original.getIssue().getId(), idempotencyKey)
                .map(RepairTaskResponse::from)
                .orElseGet(() -> createRetryTask(original, actorId, idempotencyKey, original.getRunKey()));
    }

    private RepairTaskResponse createRetryTask(
            RepairTask original, UUID actorId, String idempotencyKey, String runKey
    ) {
        UUID retryId = UUID.randomUUID();
        RepairTask retry = RepairTask.retryOf(
                retryId,
                original,
                original.getIssue().getExternalId() == null
                        ? branchNameFactory.create(original.getIssue().getGithubIssueNumber(), retryId)
                        : branchNameFactory.create(original.getIssue().getExternalId(), retryId),
                idempotencyKey
        );
        retry.assignRunKey(runKey);
        admission.reserve(actorId, retry);
        retry.transitionTo(RepairTaskStatus.QUEUED);
        retry = tasks.saveAndFlush(retry);
        admission.recordInitialStage(retry);
        appendEvent(retry, RepairTaskEventType.RETRY_CREATED, "QUEUED",
                "Mode 3 retry created from task " + original.getId(), null);
        outbox.enqueue(retry);
        return RepairTaskResponse.from(retry);
    }

    @Transactional
    public void completeSimulatedRun(UUID taskId) {
        RepairTask task = getRequired(taskId);
        if (task.getStatus() != RepairTaskStatus.QUEUED) {
            return;
        }
        task.transitionTo(RepairTaskStatus.ANALYZING);
        appendStatusEvent(task, "Simulation started analysis");
        task.transitionTo(RepairTaskStatus.FIXING);
        appendStatusEvent(task, "Simulation started fixing");
        task.transitionTo(RepairTaskStatus.VALIDATING);
        appendStatusEvent(task, "Simulation started validation");
        task.recordResult(
                "Simulation only: no repository files were changed.",
                null,
                "Simulation only: RabbitMQ delivery and task status transitions completed.",
                null
        );
        task.transitionTo(RepairTaskStatus.PATCH_READY);
        appendStatusEvent(task, "Simulation produced a patch result");
    }

    @Transactional
    public RepairTaskResponse updateFromWorker(UUID taskId, RepairTaskWorkerUpdateRequest request) {
        RepairTask task = getRequired(taskId);
        requireWorkerLeaseIfClaimed(task, request.workerId(), request.leaseToken(),
                request.effectiveStageSequence(task.getStageSequence()), request.effectiveLeaseEpoch(task.getLeaseEpoch()));
        RepairTaskStatus target = request.status();
        if (!isWorkerManagedStatus(target)) {
            throw new IllegalTaskTransitionException(task.getStatus(), target);
        }
        if (task.getStatus() == target) {
            task.recordUsage(request.modelName(), request.durationMs(), request.promptTokens(),
                    request.completionTokens(), request.totalTokens());
            return RepairTaskResponse.from(task);
        }
        if (target == RepairTaskStatus.CANCELLED && !task.isCancelRequested()) {
            throw new ConflictException("Worker cannot cancel a task without a cancellation request");
        }
        task.transitionTo(target);
        task.recordUsage(request.modelName(), request.durationMs(), request.promptTokens(),
                request.completionTokens(), request.totalTokens());
        appendStatusEvent(task, request.validationSummary());
        if (target == RepairTaskStatus.PATCH_READY
                || target == RepairTaskStatus.PR_READY
                || target == RepairTaskStatus.FAILED) {
            task.recordResult(
                    request.patchSummary(),
                    request.patchArtifactPath(),
                    request.validationSummary(),
                    request.prUrl()
            );
        }
        if (target == RepairTaskStatus.FAILED) {
            task.recordFailure(request.errorCode(), request.errorSummary(), request.retryable());
            appendEvent(task, RepairTaskEventType.ERROR, target.name(), request.errorSummary(), null);
            admission.recordStageFailure(task, request.errorCode(), request.errorSummary());
            admission.releaseTerminalCapacity(task);
        }
        if (target == RepairTaskStatus.CANCELLED) {
            task.recordCancellation(request.validationSummary());
            admission.recordStageFailure(task, "CANCELLED", request.validationSummary());
            admission.releaseTerminalCapacity(task);
        }
        return RepairTaskResponse.from(task);
    }

    @Transactional
    public RepairTaskControlResponse claim(UUID taskId, RepairTaskClaimRequest request) {
        // 领取是唯一需要抢占的入口；悲观锁令两条 RabbitMQ 重复消息只有一条能取得租约。
        RepairTask task = getRequiredForUpdate(taskId);
        if (task.isCancelRequested() || task.getStatus() == RepairTaskStatus.PATCH_READY
                || task.getStatus() == RepairTaskStatus.PR_READY
                || task.getStatus() == RepairTaskStatus.FAILED
                || task.getStatus() == RepairTaskStatus.CANCELLED) {
            throw new ConflictException("Repair task is not available for claim");
        }
        if (task.hasActiveLease() && !request.workerId().equals(task.getWorkerId())) {
            throw new ConflictException("Repair task is owned by another active worker");
        }
        task.prepareForReclaim();
        task.claimPipelineStage(
                request.effectiveStage(), request.effectiveStageSequence(task.getStageSequence()),
                request.workerId(), request.effectiveLeaseSeconds()
        );
        admission.recordClaim(task);
        appendEvent(task, RepairTaskEventType.LEASE_CLAIMED, request.effectiveStage().displayStage(),
                "Task claimed by worker " + request.workerId(), null);
        return RepairTaskControlResponse.from(task);
    }

    /**
     * The stage lease, stage transition, and next outbox event share one transaction. This makes
     * the queue boundary a recoverable handoff instead of a best-effort callback from Python.
     */
    @Transactional
    public RepairTaskControlResponse handoffStage(UUID taskId, RepairTaskStageHandoffRequest request) {
        RepairTask task = getRequiredForUpdate(taskId);
        if (task.isCancelRequested()) {
            task.requireLease(request.workerId(), request.leaseToken(),
                    request.effectiveStageSequence(task.getStageSequence()), request.effectiveLeaseEpoch(task.getLeaseEpoch()));
            task.transitionTo(RepairTaskStatus.CANCELLED);
            task.recordCancellation("Task cancelled before handoff to " + request.nextStage());
            appendStatusEvent(task, "Task cancelled before next pipeline stage");
            admission.recordStageFailure(task, "CANCELLED", "Task cancelled before handoff");
            admission.releaseTerminalCapacity(task);
            return RepairTaskControlResponse.from(task);
        }
        long completedSequence = task.getStageSequence();
        task.handoffPipelineStage(
                request.currentStage(), request.nextStage(), request.workerId(), request.leaseToken(),
                request.effectiveStageSequence(task.getStageSequence()), request.effectiveLeaseEpoch(task.getLeaseEpoch()),
                request.message()
        );
        admission.recordHandoff(task, completedSequence, request.currentStage());
        appendEvent(
                task,
                RepairTaskEventType.PROGRESS,
                request.nextStage().displayStage(),
                request.message(),
                null
        );
        outbox.enqueueStage(task, request.nextStage());
        return RepairTaskControlResponse.from(task);
    }

    @Transactional
    public RepairTaskResponse completePipeline(UUID taskId, RepairTaskPipelineCompletionRequest request) {
        RepairTask task = getRequiredForUpdate(taskId);
        task.completePipeline(request.workerId(), request.leaseToken(), request);
        admission.recordStageSucceeded(task);
        admission.releaseTerminalCapacity(task);
        appendEvent(
                task,
                RepairTaskEventType.STATUS_CHANGED,
                "COMPLETED",
                request.evaluationSummary(),
                null
        );
        return RepairTaskResponse.from(task);
    }

    @Transactional
    public RepairTaskControlResponse renewLease(UUID taskId, RepairTaskLeaseRequest request) {
        RepairTask task = getRequired(taskId);
        task.requireLease(request.workerId(), request.leaseToken(),
                request.effectiveStageSequence(task.getStageSequence()), request.effectiveLeaseEpoch(task.getLeaseEpoch()));
        task.renewLease(request.workerId(), request.leaseToken(), request.effectiveLeaseSeconds());
        return RepairTaskControlResponse.from(task);
    }

    @Transactional(readOnly = true)
    public RepairTaskControlResponse control(
            UUID taskId, String workerId, UUID leaseToken, Long stageSequence, Long leaseEpoch
    ) {
        RepairTask task = getRequired(taskId);
        task.requireLease(workerId, leaseToken,
                stageSequence == null ? task.getStageSequence() : stageSequence,
                leaseEpoch == null ? task.getLeaseEpoch() : leaseEpoch);
        return RepairTaskControlResponse.from(task);
    }

    @Transactional
    public RepairTaskEventResponse reportProgress(UUID taskId, RepairTaskProgressRequest request) {
        RepairTask task = getRequired(taskId);
        task.requireLease(request.workerId(), request.leaseToken(),
                request.effectiveStageSequence(task.getStageSequence()), request.effectiveLeaseEpoch(task.getLeaseEpoch()));
        return RepairTaskEventResponse.from(appendEvent(
                task, RepairTaskEventType.PROGRESS, request.stage(), request.message(), request.metadataJson()
        ));
    }

    public WorkerHeartbeatResponse heartbeat(String workerId, WorkerHeartbeatRequest request) {
        boolean hasInstance = request.instanceId() != null && !request.instanceId().isBlank();
        boolean hasSequence = request.sequence() != null;
        if (hasInstance != hasSequence) {
            throw new BadRequestException("instanceId and sequence must be provided together");
        }
        if (hasInstance) {
            WorkerPresenceWriteResult result = workerPresence.heartbeat(
                    workerId, request.instanceId(), request.sequence(),
                    request.role() == null || request.role().isBlank() ? "repair" : request.role(),
                    request.status(), request.activeTaskId(), request.metadataJson(), request.startedAt()
            );
            return new WorkerHeartbeatResponse(
                    workerId, request.status(), request.activeTaskId(), Instant.now(),
                    result.accepted(), result.available(), result.registrationRequired()
            );
        }
        WorkerNode worker = workers.findById(workerId).orElseGet(() -> new WorkerNode(workerId));
        worker.heartbeat(request.status(), request.activeTaskId(), request.metadataJson());
        return WorkerHeartbeatResponse.from(workers.save(worker));
    }

    @Transactional
    public WorkerHeartbeatResponse registerWorker(String workerId, WorkerRegistrationRequest request) {
        WorkerNode worker = workers.findById(workerId).orElseGet(() -> new WorkerNode(workerId));
        worker.register(request.role(), request.instanceId());
        workers.save(worker);
        WorkerPresenceWriteResult result = workerPresence.register(workerId, request.instanceId(), request.role());
        return new WorkerHeartbeatResponse(
                workerId, "IDLE", null, worker.getLastHeartbeatAt(),
                result.accepted(), result.available(), result.registrationRequired()
        );
    }

    @Transactional
    public void unregisterWorker(String workerId, String instanceId) {
        workerPresence.unregister(workerId, instanceId);
        workers.findById(workerId).ifPresent(worker -> worker.markOffline(instanceId));
    }

    @Transactional(readOnly = true)
    public RepairTaskWorkerContextResponse getWorkerContext(UUID taskId) {
        return RepairTaskWorkerContextResponse.from(getRequired(taskId));
    }

    @Transactional
    public RepairTaskResponse updateEvaluation(UUID taskId, BenchmarkEvaluationUpdateRequest request) {
        RepairTask task = getRequired(taskId);
        requireWorkerLeaseIfClaimed(task, request.workerId(), request.leaseToken(),
                request.effectiveStageSequence(task.getStageSequence()), request.effectiveLeaseEpoch(task.getLeaseEpoch()));
        if (task.getIssue().getRepository().getProvider() != RepositoryProvider.SWE_BENCH) {
            throw new BadRequestException("Benchmark evaluation is only available for SWE-bench tasks");
        }
        if (task.getStatus() != RepairTaskStatus.VALIDATING
                && task.getStatus() != RepairTaskStatus.PATCH_READY
                && task.getStatus() != RepairTaskStatus.PR_READY) {
            throw new IllegalTaskTransitionException(task.getStatus(), RepairTaskStatus.PATCH_READY);
        }
        task.recordEvaluation(request);
        appendEvent(task, RepairTaskEventType.PROGRESS, "EVALUATION_" + request.status().name(),
                request.summary(), null);
        return RepairTaskResponse.from(task);
    }

    private boolean isWorkerManagedStatus(RepairTaskStatus status) {
        return status == RepairTaskStatus.ANALYZING
                || status == RepairTaskStatus.FIXING
                || status == RepairTaskStatus.VALIDATING
                || status == RepairTaskStatus.PATCH_READY
                || status == RepairTaskStatus.PR_READY
                || status == RepairTaskStatus.FAILED
                || status == RepairTaskStatus.CANCELLED;
    }

    private boolean isRetrySource(RepairTask task) {
        return task.getStatus() == RepairTaskStatus.FAILED
                || task.getStatus() == RepairTaskStatus.CANCELLED
                || task.getStatus() == RepairTaskStatus.PATCH_READY
                || task.getStatus() == RepairTaskStatus.PR_READY;
    }

    private void requireWorkerLeaseIfClaimed(
            RepairTask task, String workerId, UUID leaseToken, long stageSequence, long leaseEpoch
    ) {
        if (task.getWorkerId() != null) {
            task.requireLease(workerId, leaseToken, stageSequence, leaseEpoch);
        }
    }

    private void appendStatusEvent(RepairTask task, String message) {
        appendEvent(task, RepairTaskEventType.STATUS_CHANGED, task.getStatus().name(), message, null);
    }

    RepairTaskEvent appendEvent(
            RepairTask task,
            RepairTaskEventType type,
            String stage,
            String message,
            String metadataJson
    ) {
        task.recordProgress(stage, message);
        RepairTaskEvent saved = taskEvents.save(new RepairTaskEvent(task, type, stage, message, metadataJson));
        Runnable publish = () -> eventStream.publish(task.getId(), RepairTaskEventResponse.from(saved));
        if (TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                @Override
                public void afterCommit() {
                    publish.run();
                }
            });
        } else {
            publish.run();
        }
        return saved;
    }

    /** 仅供恢复器使用；终态计数由 admission 的幂等标记保护。 */
    void releaseTerminalCapacity(RepairTask task) {
        admission.releaseTerminalCapacity(task);
    }

    void appendSystemEvent(RepairTask task, RepairTaskEventType type, String stage, String message) {
        appendEvent(task, type, stage, message, null);
    }

    RepairTask getRequired(UUID taskId) {
        return tasks.findById(taskId)
                .orElseThrow(() -> new NotFoundException("Repair task not found: " + taskId));
    }

    private RepairTask getRequiredForUpdate(UUID taskId) {
        return tasks.findByIdForUpdate(taskId)
                .orElseThrow(() -> new NotFoundException("Repair task not found: " + taskId));
    }
}
