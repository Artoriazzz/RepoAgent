package com.artoria.repoagentbackend.memory;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "memory_proposal")
public class MemoryProposal {
    @Id
    private UUID id;
    @Column(name = "memory_id")
    private UUID memoryId;
    @Column(name = "observation_id", nullable = false)
    private UUID observationId;
    @Column(name = "idempotency_key", nullable = false)
    private String idempotencyKey;
    @Enumerated(EnumType.STRING)
    @Column(name = "operation_type", nullable = false)
    private MemoryProposalOperation operation;
    @Column(name = "base_version")
    private Long baseVersion;
    @Enumerated(EnumType.STRING)
    @Column(name = "scope_type")
    private MemoryScopeType scopeType;
    @Column(name = "change_reason", columnDefinition = "TEXT")
    private String changeReason;
    @Column(name = "merge_batch_id")
    private UUID mergeBatchId;
    @Column(name = "proposed_payload_json", columnDefinition = "LONGTEXT")
    private String proposedPayloadJson;
    @Enumerated(EnumType.STRING)
    @Column(name = "proposal_status", nullable = false)
    private MemoryProposalStatus status;
    @Column(name = "conflict_reason", columnDefinition = "TEXT")
    private String conflictReason;
    @Column(name = "applied_revision")
    private Long appliedRevision;
    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;
    @Column(name = "processed_at")
    private Instant processedAt;

    protected MemoryProposal() {
    }

    public MemoryProposal(UUID memoryId, UUID observationId, String idempotencyKey,
                          MemoryProposalOperation operation, Long baseVersion,
                          MemoryScopeType scopeType, String changeReason, String payload) {
        this.id = UUID.randomUUID();
        this.memoryId = memoryId;
        this.observationId = observationId;
        this.idempotencyKey = idempotencyKey;
        this.operation = operation;
        this.baseVersion = baseVersion;
        this.scopeType = scopeType;
        this.changeReason = changeReason;
        this.proposedPayloadJson = payload;
        this.status = MemoryProposalStatus.PENDING;
        this.createdAt = Instant.now();
    }

    public void applied(UUID memoryId, long revision) {
        this.memoryId = memoryId;
        this.status = MemoryProposalStatus.APPLIED;
        this.appliedRevision = revision;
        this.processedAt = Instant.now();
    }

    public void processing(UUID batchId) {
        this.mergeBatchId = batchId;
        this.status = MemoryProposalStatus.PROCESSING;
        this.processedAt = null;
    }

    public void releaseForRetry() {
        this.mergeBatchId = null;
        this.status = MemoryProposalStatus.PENDING;
        this.processedAt = null;
        this.conflictReason = null;
    }

    public void stale(UUID memoryId, long currentVersion, String reason) {
        this.memoryId = memoryId;
        this.status = MemoryProposalStatus.STALE;
        this.appliedRevision = currentVersion;
        this.conflictReason = reason;
        this.processedAt = Instant.now();
    }

    public void noChangeWithoutMemory(String reason) {
        this.status = MemoryProposalStatus.NO_CHANGE;
        this.conflictReason = reason;
        this.processedAt = Instant.now();
    }

    public void conflict(UUID memoryId, long currentVersion, String reason) {
        this.memoryId = memoryId;
        this.status = MemoryProposalStatus.CONFLICT;
        this.appliedRevision = currentVersion;
        this.conflictReason = reason;
        this.processedAt = Instant.now();
    }

    public void noChange(UUID memoryId, long currentVersion) {
        this.memoryId = memoryId;
        this.status = MemoryProposalStatus.NO_CHANGE;
        this.appliedRevision = currentVersion;
        this.processedAt = Instant.now();
    }

    public void rejected(String reason) {
        this.status = MemoryProposalStatus.REJECTED;
        this.conflictReason = reason;
        this.processedAt = Instant.now();
    }

    public UUID getId() { return id; }
    public UUID getMemoryId() { return memoryId; }
    public UUID getObservationId() { return observationId; }
    public String getIdempotencyKey() { return idempotencyKey; }
    public MemoryProposalOperation getOperation() { return operation; }
    public Long getBaseVersion() { return baseVersion; }
    public MemoryScopeType getScopeType() { return scopeType; }
    public String getChangeReason() { return changeReason; }
    public UUID getMergeBatchId() { return mergeBatchId; }
    public String getProposedPayloadJson() { return proposedPayloadJson; }
    public MemoryProposalStatus getStatus() { return status; }
    public String getConflictReason() { return conflictReason; }
    public Long getAppliedRevision() { return appliedRevision; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getProcessedAt() { return processedAt; }
}
