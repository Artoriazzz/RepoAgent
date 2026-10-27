package com.artoria.repoagentbackend.memory;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;

import java.time.Duration;
import java.time.Instant;
import java.util.UUID;

/**
 * 一次合并固定在同一 memoryId + baseVersion 上。LLM 工作在事务外，提交结果时再以
 * lease token 和 baseVersion 校验归属，避免迟到的 Worker 把旧提案写进新版本。
 */
@Entity
@Table(name = "memory_merge_batch")
public class MemoryMergeBatch {
    @Id
    private UUID id;
    @Column(name = "memory_id")
    private UUID memoryId;
    @Enumerated(EnumType.STRING)
    @Column(name = "scope_type")
    private MemoryScopeType scopeType;
    @Column(name = "repository_id")
    private UUID repositoryId;
    @Enumerated(EnumType.STRING)
    @Column(name = "operation_type", nullable = false)
    private MemoryProposalOperation operation;
    @Column(name = "base_version")
    private Long baseVersion;
    @Enumerated(EnumType.STRING)
    @Column(name = "batch_status", nullable = false)
    private MemoryMergeBatchStatus status;
    @Column(name = "worker_id", nullable = false)
    private String workerId;
    @Column(name = "lease_token", nullable = false)
    private UUID leaseToken;
    @Column(name = "lease_expires_at", nullable = false)
    private Instant leaseExpiresAt;
    @Column(name = "proposal_count", nullable = false)
    private int proposalCount;
    @Column(name = "failure_reason", columnDefinition = "TEXT")
    private String failureReason;
    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;
    @Column(name = "completed_at")
    private Instant completedAt;
    @Version
    @Column(name = "row_version", nullable = false)
    private long rowVersion;

    protected MemoryMergeBatch() {
    }

    public MemoryMergeBatch(
            UUID memoryId,
            MemoryScopeType scopeType,
            UUID repositoryId,
            MemoryProposalOperation operation,
            Long baseVersion,
            int proposalCount,
            String workerId,
            UUID leaseToken,
            Duration leaseDuration
    ) {
        this.id = UUID.randomUUID();
        this.memoryId = memoryId;
        this.scopeType = scopeType;
        this.repositoryId = repositoryId;
        this.operation = operation;
        this.baseVersion = baseVersion;
        this.proposalCount = proposalCount;
        this.workerId = workerId;
        this.leaseToken = leaseToken;
        this.status = MemoryMergeBatchStatus.RUNNING;
        this.createdAt = Instant.now();
        this.updatedAt = this.createdAt;
        this.leaseExpiresAt = this.createdAt.plus(leaseDuration);
    }

    public boolean isClaimedBy(String workerId, UUID leaseToken, Instant now) {
        return status == MemoryMergeBatchStatus.RUNNING
                && this.workerId.equals(workerId)
                && this.leaseToken.equals(leaseToken)
                && leaseExpiresAt.isAfter(now);
    }

    public boolean isExpired(Instant now) {
        return status == MemoryMergeBatchStatus.RUNNING && !leaseExpiresAt.isAfter(now);
    }

    public void complete(MemoryMergeBatchStatus completedStatus) {
        this.status = completedStatus;
        this.completedAt = Instant.now();
        this.updatedAt = this.completedAt;
    }

    public void fail(String reason) {
        this.status = MemoryMergeBatchStatus.FAILED;
        this.failureReason = truncate(reason);
        this.completedAt = Instant.now();
        this.updatedAt = this.completedAt;
    }

    public UUID getId() { return id; }
    public UUID getMemoryId() { return memoryId; }
    public MemoryScopeType getScopeType() { return scopeType; }
    public UUID getRepositoryId() { return repositoryId; }
    public MemoryProposalOperation getOperation() { return operation; }
    public Long getBaseVersion() { return baseVersion; }
    public MemoryMergeBatchStatus getStatus() { return status; }
    public String getWorkerId() { return workerId; }
    public UUID getLeaseToken() { return leaseToken; }
    public Instant getLeaseExpiresAt() { return leaseExpiresAt; }
    public int getProposalCount() { return proposalCount; }
    public String getFailureReason() { return failureReason; }

    private static String truncate(String value) {
        if (value == null || value.isBlank()) return "Memory merge worker did not complete the batch";
        return value.length() <= 10_000 ? value : value.substring(value.length() - 10_000);
    }
}
