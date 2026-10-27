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
@Table(name = "memory_observation")
public class MemoryObservation {
    @Id
    private UUID id;

    @Column(name = "task_id", nullable = false)
    private UUID taskId;

    @Column(name = "repository_id", nullable = false)
    private UUID repositoryId;

    @Column(name = "idempotency_key", nullable = false)
    private String idempotencyKey;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false)
    private MemoryOutcome outcome;

    @Column(name = "problem_signature", nullable = false, columnDefinition = "TEXT")
    private String problemSignature;

    @Column(name = "patch_summary", columnDefinition = "LONGTEXT")
    private String patchSummary;

    @Column(name = "validation_summary", columnDefinition = "LONGTEXT")
    private String validationSummary;

    @Column(name = "evidence_json", nullable = false, columnDefinition = "LONGTEXT")
    private String evidenceJson;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    protected MemoryObservation() {
    }

    public MemoryObservation(
            UUID taskId,
            UUID repositoryId,
            String idempotencyKey,
            MemoryOutcome outcome,
            String problemSignature,
            String patchSummary,
            String validationSummary,
            String evidenceJson
    ) {
        this.id = UUID.randomUUID();
        this.taskId = taskId;
        this.repositoryId = repositoryId;
        this.idempotencyKey = idempotencyKey;
        this.outcome = outcome;
        this.problemSignature = problemSignature;
        this.patchSummary = patchSummary;
        this.validationSummary = validationSummary;
        this.evidenceJson = evidenceJson;
        this.createdAt = Instant.now();
    }

    public UUID getId() { return id; }
    public UUID getTaskId() { return taskId; }
    public UUID getRepositoryId() { return repositoryId; }
    public String getIdempotencyKey() { return idempotencyKey; }
    public MemoryOutcome getOutcome() { return outcome; }
    public String getProblemSignature() { return problemSignature; }
    public String getPatchSummary() { return patchSummary; }
    public String getValidationSummary() { return validationSummary; }
    public String getEvidenceJson() { return evidenceJson; }
    public Instant getCreatedAt() { return createdAt; }
}
