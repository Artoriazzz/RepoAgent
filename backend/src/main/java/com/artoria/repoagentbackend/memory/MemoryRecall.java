package com.artoria.repoagentbackend.memory;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "memory_recall")
public class MemoryRecall {
    @Id
    private UUID id;
    @Column(name = "task_id", nullable = false)
    private UUID taskId;
    @Column(name = "memory_id", nullable = false)
    private UUID memoryId;
    @Column(name = "memory_revision", nullable = false)
    private long memoryRevision;
    @Column(name = "recall_score", nullable = false)
    private double recallScore;
    @Column(name = "used_by_agent")
    private Boolean usedByAgent;
    private Boolean successful;
    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;
    @Column(name = "feedback_at")
    private Instant feedbackAt;

    protected MemoryRecall() {
    }

    public MemoryRecall(UUID taskId, UUID memoryId, long memoryRevision, double recallScore) {
        this.id = UUID.randomUUID();
        this.taskId = taskId;
        this.memoryId = memoryId;
        this.memoryRevision = memoryRevision;
        this.recallScore = recallScore;
        this.createdAt = Instant.now();
    }

    public boolean recordFeedback(boolean used, boolean success) {
        if (feedbackAt != null) return false;
        this.usedByAgent = used;
        this.successful = success;
        this.feedbackAt = Instant.now();
        return true;
    }

    public UUID getId() { return id; }
    public UUID getTaskId() { return taskId; }
    public UUID getMemoryId() { return memoryId; }
    public long getMemoryRevision() { return memoryRevision; }
    public double getRecallScore() { return recallScore; }
    public Boolean getUsedByAgent() { return usedByAgent; }
    public Boolean getSuccessful() { return successful; }
    public Instant getFeedbackAt() { return feedbackAt; }
}
