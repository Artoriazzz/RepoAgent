package com.artoria.repoagentbackend.repair;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import org.hibernate.annotations.CreationTimestamp;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "repair_user_quota")
public class RepairUserQuota {
    @Id
    @Column(name = "user_id")
    private UUID userId;
    @Column(name = "max_active_workflows", nullable = false)
    private int maxActiveWorkflows;
    @Column(name = "daily_task_limit", nullable = false)
    private int dailyTaskLimit;
    @Column(nullable = false)
    private boolean enabled;
    @CreationTimestamp @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;
    @UpdateTimestamp @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected RepairUserQuota() { }
    public RepairUserQuota(UUID userId, int maxActiveWorkflows, int dailyTaskLimit) {
        this.userId = userId;
        this.maxActiveWorkflows = maxActiveWorkflows;
        this.dailyTaskLimit = dailyTaskLimit;
        this.enabled = true;
    }
    public UUID getUserId() { return userId; }
    public int getMaxActiveWorkflows() { return maxActiveWorkflows; }
    public int getDailyTaskLimit() { return dailyTaskLimit; }
    public boolean isEnabled() { return enabled; }
}
