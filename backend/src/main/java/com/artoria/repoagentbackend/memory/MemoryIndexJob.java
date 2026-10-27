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
@Table(name = "memory_index_job")
public class MemoryIndexJob {
    @Id
    private UUID id;
    @Column(name = "memory_id", nullable = false)
    private UUID memoryId;
    @Column(name = "memory_revision", nullable = false)
    private long memoryRevision;
    @Column(name = "embedding_profile", nullable = false)
    private String embeddingProfile;
    @Enumerated(EnumType.STRING)
    @Column(name = "job_status", nullable = false)
    private MemoryIndexJobStatus status;
    @Column(nullable = false)
    private int attempts;
    @Column(name = "next_attempt_at", nullable = false)
    private Instant nextAttemptAt;
    @Column(name = "claimed_at")
    private Instant claimedAt;
    @Column(name = "last_error", columnDefinition = "TEXT")
    private String lastError;
    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected MemoryIndexJob() {
    }

    public MemoryIndexJob(UUID memoryId, long memoryRevision) {
        this(memoryId, memoryRevision, "memory-retrieval-v1");
    }

    public MemoryIndexJob(UUID memoryId, long memoryRevision, String embeddingProfile) {
        this.id = UUID.randomUUID();
        this.memoryId = memoryId;
        this.memoryRevision = memoryRevision;
        this.embeddingProfile = embeddingProfile;
        this.status = MemoryIndexJobStatus.PENDING;
        this.nextAttemptAt = Instant.now();
        this.createdAt = Instant.now();
        this.updatedAt = this.createdAt;
    }

    public void markIndexed() {
        status = MemoryIndexJobStatus.INDEXED;
        claimedAt = null;
        lastError = null;
        updatedAt = Instant.now();
    }

    public void markFailed(String message, int maxAttempts) {
        attempts++;
        lastError = truncate(message, 10_000);
        status = attempts >= maxAttempts ? MemoryIndexJobStatus.DEAD : MemoryIndexJobStatus.FAILED;
        claimedAt = null;
        long delaySeconds = Math.min(300, 1L << Math.min(Math.max(0, attempts - 1), 8));
        nextAttemptAt = Instant.now().plusSeconds(delaySeconds);
        updatedAt = Instant.now();
    }

    public boolean claim() {
        if (status != MemoryIndexJobStatus.PENDING && status != MemoryIndexJobStatus.FAILED) return false;
        status = MemoryIndexJobStatus.PROCESSING;
        claimedAt = Instant.now();
        updatedAt = claimedAt;
        return true;
    }

    private String truncate(String value, int limit) {
        if (value == null || value.length() <= limit) return value;
        return value.substring(value.length() - limit);
    }

    public UUID getId() { return id; }
    public UUID getMemoryId() { return memoryId; }
    public long getMemoryRevision() { return memoryRevision; }
    public String getEmbeddingProfile() { return embeddingProfile; }
    public MemoryIndexJobStatus getStatus() { return status; }
    public int getAttempts() { return attempts; }
    public Instant getNextAttemptAt() { return nextAttemptAt; }
    public String getLastError() { return lastError; }
    public Instant getClaimedAt() { return claimedAt; }
}
