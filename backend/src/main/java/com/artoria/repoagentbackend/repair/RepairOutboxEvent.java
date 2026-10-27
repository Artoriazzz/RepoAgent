package com.artoria.repoagentbackend.repair;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import org.hibernate.annotations.CreationTimestamp;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "repair_outbox_event")
public class RepairOutboxEvent {
    public static final String TASK_QUEUED = "REPAIR_TASK_QUEUED";

    public static String pipelineEventType(RepairPipelineStage stage, long stageSequence) {
        return "REPAIR_PIPELINE_" + stage.name() + "_" + stageSequence;
    }

    @Id
    private UUID id;

    @Column(name = "aggregate_id", nullable = false)
    private UUID aggregateId;

    @Column(name = "event_type", nullable = false)
    private String eventType;

    @Column(name = "stage_sequence", nullable = false)
    private long stageSequence;

    @Column(name = "payload_json", nullable = false, columnDefinition = "LONGTEXT")
    private String payloadJson;

    @Enumerated(EnumType.STRING)
    @Column(name = "outbox_status", nullable = false)
    private RepairOutboxStatus status;

    @Column(nullable = false)
    private int attempts;

    @Column(name = "next_attempt_at", nullable = false)
    private Instant nextAttemptAt;

    @Column(name = "published_at")
    private Instant publishedAt;

    @Column(name = "last_error", columnDefinition = "TEXT")
    private String lastError;

    @CreationTimestamp
    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @UpdateTimestamp
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected RepairOutboxEvent() {
    }

    public RepairOutboxEvent(UUID aggregateId, String eventType, String payloadJson) {
        this(aggregateId, eventType, payloadJson, 0);
    }

    public RepairOutboxEvent(UUID aggregateId, String eventType, String payloadJson, long stageSequence) {
        this.id = UUID.randomUUID();
        this.aggregateId = aggregateId;
        this.eventType = eventType;
        this.stageSequence = stageSequence;
        this.payloadJson = payloadJson;
        this.status = RepairOutboxStatus.PENDING;
        this.nextAttemptAt = Instant.now();
    }

    public void markPublished() {
        this.status = RepairOutboxStatus.PUBLISHED;
        this.publishedAt = Instant.now();
        this.lastError = null;
    }

    public void markFailed(String error, int maxAttempts) {
        attempts++;
        lastError = truncate(error, 20_000);
        if (attempts >= maxAttempts) {
            status = RepairOutboxStatus.DEAD;
            return;
        }
        status = RepairOutboxStatus.FAILED;
        long delaySeconds = Math.min(300, 1L << Math.min(attempts - 1, 8));
        nextAttemptAt = Instant.now().plusSeconds(delaySeconds);
    }

    public void reschedule(String payloadJson) {
        this.payloadJson = payloadJson;
        this.status = RepairOutboxStatus.PENDING;
        this.attempts = 0;
        this.nextAttemptAt = Instant.now();
        this.publishedAt = null;
        this.lastError = null;
    }

    private static String truncate(String value, int limit) {
        if (value == null || value.length() <= limit) return value;
        return value.substring(value.length() - limit);
    }

    public UUID getId() { return id; }
    public UUID getAggregateId() { return aggregateId; }
    public String getEventType() { return eventType; }
    public long getStageSequence() { return stageSequence; }
    public String getPayloadJson() { return payloadJson; }
    public RepairOutboxStatus getStatus() { return status; }
    public int getAttempts() { return attempts; }
    public Instant getNextAttemptAt() { return nextAttemptAt; }
    public Instant getPublishedAt() { return publishedAt; }
    public String getLastError() { return lastError; }
    public Instant getCreatedAt() { return createdAt; }
}
