package com.artoria.repoagentbackend.repair;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.FetchType;
import jakarta.persistence.Id;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;
import jakarta.persistence.Table;
import org.hibernate.annotations.UuidGenerator;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "repair_task_event")
public class RepairTaskEvent {
    @Id
    @UuidGenerator
    private UUID id;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "task_id", nullable = false)
    private RepairTask task;

    @Enumerated(EnumType.STRING)
    @Column(name = "event_type", nullable = false)
    private RepairTaskEventType eventType;

    @Column(name = "stage")
    private String stage;

    @Column(name = "message", columnDefinition = "TEXT")
    private String message;

    @Column(name = "metadata_json", columnDefinition = "LONGTEXT")
    private String metadataJson;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    protected RepairTaskEvent() {
    }

    public RepairTaskEvent(
            RepairTask task,
            RepairTaskEventType eventType,
            String stage,
            String message,
            String metadataJson
    ) {
        this.task = task;
        this.eventType = eventType;
        this.stage = stage;
        this.message = message;
        this.metadataJson = metadataJson;
        this.createdAt = Instant.now();
    }

    public UUID getId() { return id; }
    public RepairTask getTask() { return task; }
    public RepairTaskEventType getEventType() { return eventType; }
    public String getStage() { return stage; }
    public String getMessage() { return message; }
    public String getMetadataJson() { return metadataJson; }
    public Instant getCreatedAt() { return createdAt; }
}
