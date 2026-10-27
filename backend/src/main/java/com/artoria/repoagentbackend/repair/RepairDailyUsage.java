package com.artoria.repoagentbackend.repair;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;
import jakarta.persistence.Version;
import org.hibernate.annotations.CreationTimestamp;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;
import java.time.LocalDate;
import java.util.UUID;

@Entity
@Table(name = "repair_daily_usage", uniqueConstraints = @UniqueConstraint(
        name = "uk_repair_daily_user_date", columnNames = {"user_id", "usage_date"}
))
public class RepairDailyUsage {
    @Id private UUID id;
    @Column(name = "user_id", nullable = false) private UUID userId;
    @Column(name = "usage_date", nullable = false) private LocalDate usageDate;
    @Column(name = "accepted_count", nullable = false) private int acceptedCount;
    @Version @Column(name = "row_version", nullable = false) private long rowVersion;
    @CreationTimestamp @Column(name = "created_at", nullable = false, updatable = false) private Instant createdAt;
    @UpdateTimestamp @Column(name = "updated_at", nullable = false) private Instant updatedAt;
    protected RepairDailyUsage() { }
    public RepairDailyUsage(UUID userId, LocalDate usageDate) {
        this.id = UUID.randomUUID(); this.userId = userId; this.usageDate = usageDate;
    }
    public int getAcceptedCount() { return acceptedCount; }
    public void accept() { acceptedCount++; }
}
