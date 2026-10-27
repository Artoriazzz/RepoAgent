package com.artoria.repoagentbackend.repair;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;

@Entity
@Table(name = "repair_system_capacity")
public class RepairSystemCapacity {
    public static final String GLOBAL = "GLOBAL";
    @Id @Column(name = "capacity_key") private String capacityKey;
    @Column(name = "active_workflows", nullable = false) private int activeWorkflows;
    @Version @Column(name = "row_version", nullable = false) private long rowVersion;
    @UpdateTimestamp @Column(name = "updated_at", nullable = false) private Instant updatedAt;
    protected RepairSystemCapacity() { }
    public RepairSystemCapacity(String capacityKey) { this.capacityKey = capacityKey; }
    public int getActiveWorkflows() { return activeWorkflows; }
    public void reserve() { activeWorkflows++; }
    public void release() { activeWorkflows = Math.max(0, activeWorkflows - 1); }
}
