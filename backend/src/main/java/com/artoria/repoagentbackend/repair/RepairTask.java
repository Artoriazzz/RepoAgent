package com.artoria.repoagentbackend.repair;

import com.artoria.repoagentbackend.issue.RepositoryIssue;
import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.FetchType;
import jakarta.persistence.Id;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import org.hibernate.annotations.CreationTimestamp;
import org.hibernate.annotations.SQLRestriction;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "repair_task")
@SQLRestriction("deleted_at IS NULL")
public class RepairTask {

    @Id
    private UUID id;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "issue_id", nullable = false)
    private RepositoryIssue issue;

    @Column(name = "base_sha", nullable = false)
    private String baseSha;

    @Column(name = "branch_name", nullable = false, unique = true)
    private String branchName;

    @Enumerated(EnumType.STRING)
    @Column(name = "task_status", nullable = false)
    private RepairTaskStatus status;

    @Column(name = "idempotency_key", nullable = false)
    private String idempotencyKey;

    /** 提交者是配额和并发名额的归属，不等同于仓库当前 owner。 */
    @Column(name = "submitted_by_user_id")
    private UUID submittedByUserId;

    @Enumerated(EnumType.STRING)
    @Column(name = "workflow_status", length = 32)
    private RepairWorkflowStatus workflowStatus;

    @Enumerated(EnumType.STRING)
    @Column(name = "current_pipeline_stage", length = 64)
    private RepairPipelineStage currentPipelineStage;

    @Enumerated(EnumType.STRING)
    @Column(name = "stage_status", length = 32)
    private RepairStageStatus stageStatus;

    @Column(name = "stage_sequence", nullable = false)
    private long stageSequence;

    @Column(name = "lease_epoch", nullable = false)
    private long leaseEpoch;

    @Column(name = "capacity_released_at")
    private Instant capacityReleasedAt;

    @Column(name = "admitted_at")
    private Instant admittedAt;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "parent_task_id")
    private RepairTask parentTask;

    @Column(name = "attempt_number", nullable = false)
    private Integer attemptNumber;

    @Column(name = "model_name")
    private String modelName;

    @Column(name = "run_key")
    private String runKey;

    @Column(name = "patch_summary", columnDefinition = "TEXT")
    private String patchSummary;

    @Column(name = "patch_artifact_path")
    private String patchArtifactPath;

    @Column(name = "validation_summary", columnDefinition = "TEXT")
    private String validationSummary;

    @Column(name = "pr_url")
    private String prUrl;

    @Column(name = "completed_at")
    private Instant completedAt;

    @Enumerated(EnumType.STRING)
    @Column(name = "evaluation_status", nullable = false)
    private BenchmarkEvaluationStatus evaluationStatus;

    @Column(name = "fail_to_pass_passed")
    private Integer failToPassPassed;

    @Column(name = "fail_to_pass_failed")
    private Integer failToPassFailed;

    @Column(name = "pass_to_pass_passed")
    private Integer passToPassPassed;

    @Column(name = "pass_to_pass_failed")
    private Integer passToPassFailed;

    @Column(name = "evaluation_summary", columnDefinition = "TEXT")
    private String evaluationSummary;

    @Column(name = "harness_log_path")
    private String harnessLogPath;

    @Column(name = "evaluated_at")
    private Instant evaluatedAt;

    @Column(name = "error_code")
    private String errorCode;

    @Column(name = "error_summary", columnDefinition = "TEXT")
    private String errorSummary;

    @Column(name = "retryable")
    private Boolean retryable;

    @Column(name = "cancel_requested_at")
    private Instant cancelRequestedAt;

    @Column(name = "worker_id")
    private String workerId;

    @Column(name = "lease_token")
    private UUID leaseToken;

    @Column(name = "lease_expires_at")
    private Instant leaseExpiresAt;

    @Column(name = "last_heartbeat_at")
    private Instant lastHeartbeatAt;

    @Column(name = "duration_ms")
    private Long durationMs;

    @Column(name = "prompt_tokens")
    private Long promptTokens;

    @Column(name = "completion_tokens")
    private Long completionTokens;

    @Column(name = "total_tokens")
    private Long totalTokens;

    @Column(name = "current_stage", length = 64)
    private String currentStage;

    @Column(name = "current_message", columnDefinition = "TEXT")
    private String currentMessage;

    @Enumerated(EnumType.STRING)
    @Column(name = "pipeline_stage", nullable = false, length = 64)
    private RepairPipelineStage pipelineStage;

    @Column(name = "correction_attempt", nullable = false)
    private Integer correctionAttempt;

    @Column(name = "max_correction_attempts", nullable = false)
    private Integer maxCorrectionAttempts;

    @Column(name = "deleted_at")
    private Instant deletedAt;

    @Version
    @Column(nullable = false)
    private Long version;

    @CreationTimestamp
    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @UpdateTimestamp
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected RepairTask() {
    }

    public RepairTask(RepositoryIssue issue, String baseSha, String branchName, String idempotencyKey) {
        this(UUID.randomUUID(), issue, baseSha, branchName, idempotencyKey);
    }

    public RepairTask(
            UUID id,
            RepositoryIssue issue,
            String baseSha,
            String branchName,
            String idempotencyKey
    ) {
        this.id = id;
        this.issue = issue;
        this.baseSha = baseSha;
        this.branchName = branchName;
        this.idempotencyKey = idempotencyKey;
        this.status = RepairTaskStatus.CREATED;
        this.evaluationStatus = BenchmarkEvaluationStatus.NOT_RUN;
        this.attemptNumber = 1;
        this.correctionAttempt = 0;
        this.maxCorrectionAttempts = 1;
        this.currentStage = "QUEUED";
        this.pipelineStage = RepairPipelineStage.COORDINATOR;
        this.currentPipelineStage = RepairPipelineStage.COORDINATOR;
        this.workflowStatus = RepairWorkflowStatus.RUNNING;
        this.stageStatus = RepairStageStatus.WAITING;
        this.stageSequence = 0;
        this.leaseEpoch = 0;
    }

    public static RepairTask retryOf(
            UUID id,
            RepairTask original,
            String branchName,
            String idempotencyKey
    ) {
        RepairTask retry = new RepairTask(id, original.issue, original.baseSha, branchName, idempotencyKey);
        retry.parentTask = original;
        retry.attemptNumber = original.getAttemptNumber() + 1;
        retry.modelName = original.modelName;
        retry.runKey = original.runKey;
        retry.submittedByUserId = original.submittedByUserId;
        return retry;
    }

    /** 只有 Admission 事务成功后才能调用，确保容量计数和任务一起提交。 */
    public void admit(UUID actorId) {
        this.submittedByUserId = actorId;
        this.workflowStatus = RepairWorkflowStatus.RUNNING;
        this.currentPipelineStage = RepairPipelineStage.COORDINATOR;
        this.pipelineStage = RepairPipelineStage.COORDINATOR;
        this.stageStatus = RepairStageStatus.WAITING;
        this.stageSequence = 0;
        this.admittedAt = Instant.now();
    }

    public void transitionTo(RepairTaskStatus target) {
        if (!status.canTransitionTo(target)) {
            throw new IllegalTaskTransitionException(status, target);
        }
        status = target;
        if (target == RepairTaskStatus.CANCELLED) {
            workflowStatus = RepairWorkflowStatus.CANCELLED;
        } else if (target == RepairTaskStatus.FAILED) {
            workflowStatus = RepairWorkflowStatus.FAILED;
            stageStatus = RepairStageStatus.FAILED;
        }
    }

    public void recordProgress(String stage, String message) {
        if (stage != null && !stage.isBlank()) {
            this.currentStage = "EVALUATION_RUNNING".equals(stage)
                    ? "FINAL_EVALUATION"
                    : stage.startsWith("EVALUATION_") ? "COMPLETED" : stage;
            if ("CORRECTIVE_IMPLEMENT".equals(stage)) {
                this.correctionAttempt = Math.min(1, getMaxCorrectionAttempts());
            }
        }
        if (message != null && !message.isBlank()) {
            this.currentMessage = message;
        }
    }

    public void handoffPipelineStage(
            RepairPipelineStage current,
            RepairPipelineStage next,
            String workerId,
            UUID leaseToken,
            String message
    ) {
        handoffPipelineStage(current, next, workerId, leaseToken, stageSequence, leaseEpoch, message);
    }

    public void handoffPipelineStage(
            RepairPipelineStage current,
            RepairPipelineStage next,
            String workerId,
            UUID leaseToken,
            long expectedStageSequence,
            long expectedLeaseEpoch,
            String message
    ) {
        requireLease(workerId, leaseToken, expectedStageSequence, expectedLeaseEpoch);
        if (getPipelineStage() != current) {
            throw new RepairTaskLeaseException(
                    "Repair task is at pipeline stage " + pipelineStage + ", not " + current
            );
        }
        if (!current.canHandoffTo(next)) {
            throw new IllegalArgumentException("Invalid repair pipeline handoff: " + current + " -> " + next);
        }
        // 交接只表示消息已投递到下一个角色队列，并不代表该角色已经开始执行。
        if (status != RepairTaskStatus.QUEUED) {
            transitionTo(RepairTaskStatus.QUEUED);
        }
        pipelineStage = next;
        currentPipelineStage = next;
        stageSequence++;
        stageStatus = RepairStageStatus.WAITING;
        if (next == RepairPipelineStage.CORRECTIVE_IMPLEMENT) {
            correctionAttempt = Math.min(1, getMaxCorrectionAttempts());
        }
        recordProgress(next.displayStage(), message);
        clearLease();
    }

    public void completePipeline(
            String workerId,
            UUID leaseToken,
            RepairTaskPipelineCompletionRequest request
    ) {
        requireLease(workerId, leaseToken, request.effectiveStageSequence(stageSequence), request.effectiveLeaseEpoch(leaseEpoch));
        if (getPipelineStage() != RepairPipelineStage.EXPERIENCE_MEMORY) {
            throw new RepairTaskLeaseException("Repair task is not ready for pipeline completion");
        }
        if (status != RepairTaskStatus.PATCH_READY) {
            transitionTo(RepairTaskStatus.PATCH_READY);
        }
        recordResult(
                request.patchSummary(), request.patchArtifactPath(), request.validationSummary(), null
        );
        recordUsage(
                request.modelName(), request.durationMs(), request.promptTokens(),
                request.completionTokens(), request.totalTokens()
        );
        recordEvaluation(new BenchmarkEvaluationUpdateRequest(
                request.evaluationStatus(),
                request.failToPassPassed(), request.failToPassFailed(),
                request.passToPassPassed(), request.passToPassFailed(),
                request.evaluationSummary(), request.harnessLogPath(), workerId, leaseToken,
                request.stageSequence(), request.leaseEpoch()
        ));
        recordProgress("COMPLETED", "Repair pipeline completed after experience memory processing");
        stageStatus = RepairStageStatus.SUCCEEDED;
        workflowStatus = request.evaluationStatus() == BenchmarkEvaluationStatus.RESOLVED
                ? RepairWorkflowStatus.SUCCEEDED : RepairWorkflowStatus.FAILED;
        clearLease();
    }

    private void transitionForPipelineStage(RepairPipelineStage next) {
        RepairTaskStatus target = switch (next) {
            case COORDINATOR -> RepairTaskStatus.ANALYZING;
            case IMPLEMENT, FAILURE_REVIEW, CORRECTIVE_IMPLEMENT -> RepairTaskStatus.FIXING;
            case VALIDATION, EXPERIENCE_MEMORY -> RepairTaskStatus.VALIDATING;
        };
        if (status != target) {
            transitionTo(target);
        }
    }

    public RepairResultStatus getResultStatus() {
        if (workflowStatus == RepairWorkflowStatus.CANCELLED) return RepairResultStatus.CANCELLED;
        if (workflowStatus == RepairWorkflowStatus.SUCCEEDED) return RepairResultStatus.SUCCEEDED;
        if (workflowStatus == RepairWorkflowStatus.FAILED) {
            return evaluationStatus == BenchmarkEvaluationStatus.HARNESS_ERROR
                    ? RepairResultStatus.ENVIRONMENT_ERROR : RepairResultStatus.FAILED;
        }
        if (status == RepairTaskStatus.CANCELLED) return RepairResultStatus.CANCELLED;
        if (evaluationStatus == BenchmarkEvaluationStatus.RESOLVED) return RepairResultStatus.SUCCEEDED;
        if (evaluationStatus == BenchmarkEvaluationStatus.UNRESOLVED) return RepairResultStatus.FAILED;
        if (evaluationStatus == BenchmarkEvaluationStatus.HARNESS_ERROR) {
            return RepairResultStatus.ENVIRONMENT_ERROR;
        }
        if (status == RepairTaskStatus.PATCH_READY
                && evaluationStatus == BenchmarkEvaluationStatus.NOT_RUN
                && workerId == null
                && completedAt != null) {
            return RepairResultStatus.ENVIRONMENT_ERROR;
        }
        if (status == RepairTaskStatus.FAILED) return RepairResultStatus.FAILED;
        return RepairResultStatus.PENDING;
    }

    public void markDeleted() {
        this.deletedAt = Instant.now();
    }

    public void recordResult(
            String patchSummary,
            String patchArtifactPath,
            String validationSummary,
            String prUrl
    ) {
        this.patchSummary = patchSummary;
        this.patchArtifactPath = patchArtifactPath;
        this.validationSummary = validationSummary;
        this.prUrl = prUrl;
        this.completedAt = Instant.now();
    }

    public void recordEvaluation(BenchmarkEvaluationUpdateRequest request) {
        this.evaluationStatus = request.status();
        this.failToPassPassed = request.failToPassPassed();
        this.failToPassFailed = request.failToPassFailed();
        this.passToPassPassed = request.passToPassPassed();
        this.passToPassFailed = request.passToPassFailed();
        this.evaluationSummary = request.summary();
        this.harnessLogPath = request.harnessLogPath();
        this.evaluatedAt = request.status() == BenchmarkEvaluationStatus.RUNNING ? null : Instant.now();
    }

    public void requestCancellation() {
        if (cancelRequestedAt == null) {
            cancelRequestedAt = Instant.now();
        }
    }

    public void recordCancellation(String summary) {
        if (summary != null) {
            this.validationSummary = summary;
        }
        this.completedAt = Instant.now();
    }

    public UUID claim(String workerId, int leaseSeconds) {
        Instant now = Instant.now();
        if (this.workerId != null && leaseExpiresAt != null && leaseExpiresAt.isAfter(now)) {
            if (!this.workerId.equals(workerId)) {
                throw new RepairTaskLeaseException("Repair task already has an active worker lease");
            }
        }
        this.workerId = workerId;
        this.leaseToken = UUID.randomUUID();
        this.leaseEpoch++;
        this.lastHeartbeatAt = now;
        this.leaseExpiresAt = now.plusSeconds(leaseSeconds);
        return leaseToken;
    }

    public UUID claimPipelineStage(
            RepairPipelineStage stage,
            String workerId,
            int leaseSeconds
    ) {
        return claimPipelineStage(stage, stageSequence, workerId, leaseSeconds);
    }

    public UUID claimPipelineStage(
            RepairPipelineStage stage,
            long expectedStageSequence,
            String workerId,
            int leaseSeconds
    ) {
        if (workflowStatus != null && workflowStatus != RepairWorkflowStatus.RUNNING) {
            throw new RepairTaskLeaseException("Repair workflow is no longer running");
        }
        if (getPipelineStage() != stage) {
            throw new RepairTaskLeaseException(
                    "Repair task is at pipeline stage " + getPipelineStage() + ", not " + stage
            );
        }
        if (expectedStageSequence != stageSequence) {
            throw new RepairTaskLeaseException("Repair task message belongs to an obsolete stage sequence");
        }
        if (stageStatus != null && stageStatus != RepairStageStatus.WAITING) {
            throw new RepairTaskLeaseException("Repair task stage is not waiting for a worker");
        }
        transitionForPipelineStage(stage);
        UUID token = claim(workerId, leaseSeconds);
        stageStatus = RepairStageStatus.RUNNING;
        return token;
    }

    public boolean hasActiveLease() {
        return workerId != null && leaseToken != null
                && leaseExpiresAt != null && leaseExpiresAt.isAfter(Instant.now());
    }

    public void prepareForReclaim() {
        if (status == RepairTaskStatus.ANALYZING
                || status == RepairTaskStatus.FIXING
                || status == RepairTaskStatus.VALIDATING) {
            status = RepairTaskStatus.QUEUED;
        }
    }

    public boolean recoverExpiredLease() {
        return recoverExpiredLease(Instant.now());
    }

    boolean recoverExpiredLease(Instant now) {
        if (leaseExpiresAt == null || leaseExpiresAt.isAfter(now)) {
            throw new RepairTaskLeaseException("Repair task lease has not expired");
        }
        if (status == RepairTaskStatus.PATCH_READY) {
            if (evaluationStatus == BenchmarkEvaluationStatus.RUNNING) {
                evaluationStatus = BenchmarkEvaluationStatus.HARNESS_ERROR;
                evaluationSummary = "Worker lease expired while the benchmark harness was running";
                evaluatedAt = now;
            }
            clearLease();
            return false;
        }
        if (status != RepairTaskStatus.QUEUED
                && status != RepairTaskStatus.ANALYZING
                && status != RepairTaskStatus.FIXING
                && status != RepairTaskStatus.VALIDATING) {
            throw new RepairTaskLeaseException("Repair task is not in a recoverable state");
        }
        status = RepairTaskStatus.QUEUED;
        stageStatus = RepairStageStatus.WAITING;
        clearLease();
        return true;
    }

    public void cancelAfterLeaseExpired() {
        cancelAfterLeaseExpired(Instant.now());
    }

    void cancelAfterLeaseExpired(Instant now) {
        if (!isCancelRequested()) {
            throw new IllegalStateException("Repair task has no cancellation request");
        }
        if (leaseExpiresAt != null && leaseExpiresAt.isAfter(now)) {
            throw new RepairTaskLeaseException("Repair task lease has not expired");
        }
        transitionTo(RepairTaskStatus.CANCELLED);
        workflowStatus = RepairWorkflowStatus.CANCELLED;
        recordCancellation("Task cancelled after worker lease expired");
        clearLease();
    }

    private void clearLease() {
        workerId = null;
        leaseToken = null;
        leaseExpiresAt = null;
        lastHeartbeatAt = null;
    }

    public void renewLease(String workerId, UUID leaseToken, int leaseSeconds) {
        requireLease(workerId, leaseToken);
        Instant now = Instant.now();
        this.lastHeartbeatAt = now;
        this.leaseExpiresAt = now.plusSeconds(leaseSeconds);
    }

    public void requireLease(String workerId, UUID leaseToken) {
        if (this.workerId == null || this.leaseToken == null
                || !this.workerId.equals(workerId) || !this.leaseToken.equals(leaseToken)) {
            throw new RepairTaskLeaseException("Worker does not own the repair task lease");
        }
        if (leaseExpiresAt == null || !leaseExpiresAt.isAfter(Instant.now())) {
            throw new RepairTaskLeaseException("Repair task lease has expired");
        }
    }

    public void requireLease(String workerId, UUID leaseToken, long expectedStageSequence, long expectedLeaseEpoch) {
        requireLease(workerId, leaseToken);
        if (stageSequence != expectedStageSequence || leaseEpoch != expectedLeaseEpoch) {
            throw new RepairTaskLeaseException("Worker lease fencing token no longer matches the current stage");
        }
    }

    public void markWorkflowFailed(String errorCode, String errorSummary) {
        workflowStatus = RepairWorkflowStatus.FAILED;
        stageStatus = RepairStageStatus.FAILED;
        recordFailure(errorCode, errorSummary, false);
        if (status != RepairTaskStatus.FAILED) transitionTo(RepairTaskStatus.FAILED);
        clearLease();
    }

    /** 返回 true 仅一次，调用方据此释放用户和全局并发槽位。 */
    public boolean markCapacityReleasedIfTerminal() {
        if (workflowStatus == null || !workflowStatus.isTerminal() || capacityReleasedAt != null) return false;
        capacityReleasedAt = Instant.now();
        return true;
    }

    public void recordFailure(String errorCode, String errorSummary, Boolean retryable) {
        this.errorCode = errorCode;
        this.errorSummary = errorSummary;
        this.retryable = retryable;
    }

    public void recordUsage(String modelName, Long durationMs, Long promptTokens, Long completionTokens, Long totalTokens) {
        if (modelName != null) this.modelName = modelName;
        if (durationMs != null) this.durationMs = durationMs;
        if (promptTokens != null) this.promptTokens = promptTokens;
        if (completionTokens != null) this.completionTokens = completionTokens;
        if (totalTokens != null) this.totalTokens = totalTokens;
    }

    public void assignRunKey(String runKey) {
        this.runKey = runKey;
    }

    public UUID getId() { return id; }
    public UUID getSubmittedByUserId() { return submittedByUserId; }
    public RepairWorkflowStatus getWorkflowStatus() {
        if (workflowStatus != null) return workflowStatus;
        return getResultStatusLegacy() == RepairResultStatus.PENDING ? RepairWorkflowStatus.RUNNING
                : status == RepairTaskStatus.CANCELLED ? RepairWorkflowStatus.CANCELLED
                : getResultStatusLegacy() == RepairResultStatus.SUCCEEDED ? RepairWorkflowStatus.SUCCEEDED
                : RepairWorkflowStatus.FAILED;
    }
    private RepairResultStatus getResultStatusLegacy() {
        if (status == RepairTaskStatus.CANCELLED) return RepairResultStatus.CANCELLED;
        if (evaluationStatus == BenchmarkEvaluationStatus.RESOLVED) return RepairResultStatus.SUCCEEDED;
        if (evaluationStatus == BenchmarkEvaluationStatus.UNRESOLVED || status == RepairTaskStatus.FAILED) return RepairResultStatus.FAILED;
        return RepairResultStatus.PENDING;
    }
    public RepairPipelineStage getCurrentPipelineStage() { return currentPipelineStage == null ? pipelineStage : currentPipelineStage; }
    public RepairStageStatus getStageStatus() { return stageStatus == null ? RepairStageStatus.WAITING : stageStatus; }
    public long getStageSequence() { return stageSequence; }
    public long getLeaseEpoch() { return leaseEpoch; }
    public Instant getCapacityReleasedAt() { return capacityReleasedAt; }
    public Instant getAdmittedAt() { return admittedAt; }
    public RepositoryIssue getIssue() { return issue; }
    public String getBaseSha() { return baseSha; }
    public String getBranchName() { return branchName; }
    public RepairTaskStatus getStatus() { return status; }
    public String getPatchSummary() { return patchSummary; }
    public String getPatchArtifactPath() { return patchArtifactPath; }
    public String getValidationSummary() { return validationSummary; }
    public String getPrUrl() { return prUrl; }
    public Instant getCompletedAt() { return completedAt; }
    public BenchmarkEvaluationStatus getEvaluationStatus() { return evaluationStatus; }
    public Integer getFailToPassPassed() { return failToPassPassed; }
    public Integer getFailToPassFailed() { return failToPassFailed; }
    public Integer getPassToPassPassed() { return passToPassPassed; }
    public Integer getPassToPassFailed() { return passToPassFailed; }
    public String getEvaluationSummary() { return evaluationSummary; }
    public String getHarnessLogPath() { return harnessLogPath; }
    public Instant getEvaluatedAt() { return evaluatedAt; }
    public UUID getParentTaskId() { return parentTask == null ? null : parentTask.getId(); }
    public String getPreviousFailureSummary() {
        if (parentTask == null) return null;
        if (parentTask.evaluationSummary != null && !parentTask.evaluationSummary.isBlank()) {
            return parentTask.evaluationSummary;
        }
        if (parentTask.errorSummary != null && !parentTask.errorSummary.isBlank()) {
            return parentTask.errorSummary;
        }
        return parentTask.validationSummary;
    }
    public int getAttemptNumber() { return attemptNumber == null ? 1 : attemptNumber; }
    public String getModelName() { return modelName; }
    public String getRunKey() { return runKey; }
    public String getErrorCode() { return errorCode; }
    public String getErrorSummary() { return errorSummary; }
    public Boolean getRetryable() { return retryable; }
    public Instant getCancelRequestedAt() { return cancelRequestedAt; }
    public boolean isCancelRequested() { return cancelRequestedAt != null; }
    public String getWorkerId() { return workerId; }
    public UUID getLeaseToken() { return leaseToken; }
    public Instant getLeaseExpiresAt() { return leaseExpiresAt; }
    public Instant getLastHeartbeatAt() { return lastHeartbeatAt; }
    public Long getDurationMs() { return durationMs; }
    public Long getPromptTokens() { return promptTokens; }
    public Long getCompletionTokens() { return completionTokens; }
    public Long getTotalTokens() { return totalTokens; }
    public String getCurrentStage() { return currentStage; }
    public String getCurrentMessage() { return currentMessage; }
    public RepairPipelineStage getPipelineStage() { return currentPipelineStage == null ? pipelineStage : currentPipelineStage; }
    public int getCorrectionAttempt() { return correctionAttempt == null ? 0 : correctionAttempt; }
    public int getMaxCorrectionAttempts() {
        return maxCorrectionAttempts == null ? 1 : maxCorrectionAttempts;
    }
    public long getVersion() { return version == null ? 0 : version; }
    public Instant getCreatedAt() { return createdAt; }
}
