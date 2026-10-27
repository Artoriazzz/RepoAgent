package com.artoria.repoagentbackend.repair;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;
import jakarta.persistence.Version;
import org.hibernate.annotations.CreationTimestamp;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;
import java.util.UUID;

/** 每一次 durable stage handoff 的不可变审计行，便于恢复与 ETA 统计。 */
@Entity
@Table(name = "repair_stage_execution", uniqueConstraints = @UniqueConstraint(
        name = "uk_repair_stage_sequence", columnNames = {"task_id", "stage_sequence"}
))
public class RepairStageExecution {
    @Id private UUID id;
    @Column(name = "task_id", nullable = false) private UUID taskId;
    @Column(name = "stage_sequence", nullable = false) private long stageSequence;
    @Enumerated(EnumType.STRING) @Column(name = "pipeline_stage", nullable = false) private RepairPipelineStage pipelineStage;
    @Enumerated(EnumType.STRING) @Column(name = "stage_status", nullable = false) private RepairStageStatus stageStatus;
    @Column(name = "lease_epoch", nullable = false) private long leaseEpoch;
    @Column(name = "worker_id") private String workerId;
    @Column(name = "started_at") private Instant startedAt;
    @Column(name = "finished_at") private Instant finishedAt;
    @Column(name = "duration_ms") private Long durationMs;
    @Column(name = "error_code") private String errorCode;
    @Column(name = "error_summary", columnDefinition = "TEXT") private String errorSummary;
    @Version @Column(name = "row_version", nullable = false) private long rowVersion;
    @CreationTimestamp @Column(name = "created_at", nullable = false, updatable = false) private Instant createdAt;
    @UpdateTimestamp @Column(name = "updated_at", nullable = false) private Instant updatedAt;
    protected RepairStageExecution() { }
    public RepairStageExecution(UUID taskId, long sequence, RepairPipelineStage stage) {
        this.id = UUID.randomUUID(); this.taskId = taskId; this.stageSequence = sequence;
        this.pipelineStage = stage; this.stageStatus = RepairStageStatus.WAITING;
    }
    public void start(long epoch, String workerId) {
        this.leaseEpoch = epoch; this.workerId = workerId; this.stageStatus = RepairStageStatus.RUNNING;
        this.startedAt = Instant.now();
    }
    public void succeed() { finish(RepairStageStatus.SUCCEEDED, null, null); }
    public void fail(String code, String summary) { finish(RepairStageStatus.FAILED, code, summary); }
    private void finish(RepairStageStatus status, String code, String summary) {
        this.stageStatus = status; this.finishedAt = Instant.now(); this.errorCode = code; this.errorSummary = summary;
        this.durationMs = startedAt == null ? null : Math.max(0, finishedAt.toEpochMilli() - startedAt.toEpochMilli());
    }
    public long getStageSequence() { return stageSequence; }
    public RepairPipelineStage getPipelineStage() { return pipelineStage; }
    public RepairStageStatus getStageStatus() { return stageStatus; }
    public Long getDurationMs() { return durationMs; }
}
