package com.artoria.repoagentbackend.repair;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "repair_user_runtime")
public class RepairUserRuntime {
    @Id @Column(name = "user_id")
    private UUID userId;
    @Column(name = "active_workflows", nullable = false)
    private int activeWorkflows;
    @Version @Column(name = "row_version", nullable = false)
    private long rowVersion;
    @UpdateTimestamp @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;
    protected RepairUserRuntime() { }
    public RepairUserRuntime(UUID userId) { this.userId = userId; }
    public UUID getUserId() { return userId; }
    public int getActiveWorkflows() { return activeWorkflows; }
    public void reserve() { activeWorkflows++; }
    public void release() { activeWorkflows = Math.max(0, activeWorkflows - 1); }
}
