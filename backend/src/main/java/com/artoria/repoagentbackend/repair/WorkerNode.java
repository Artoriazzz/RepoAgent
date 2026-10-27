package com.artoria.repoagentbackend.repair;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "worker_node")
public class WorkerNode {
    @Id
    @Column(name = "worker_id")
    private String workerId;

    @Column(name = "status", nullable = false)
    private String status;

    @Column(name = "active_task_id")
    private UUID activeTaskId;

    @Column(name = "metadata_json", columnDefinition = "LONGTEXT")
    private String metadataJson;

    @Column(name = "worker_role")
    private String workerRole;

    @Column(name = "instance_id")
    private String instanceId;

    @Column(name = "last_heartbeat_at", nullable = false)
    private Instant lastHeartbeatAt;

    protected WorkerNode() {
    }

    public WorkerNode(String workerId) {
        this.workerId = workerId;
    }

    public void heartbeat(String status, UUID activeTaskId, String metadataJson) {
        this.status = status;
        this.activeTaskId = activeTaskId;
        this.metadataJson = metadataJson;
        this.lastHeartbeatAt = Instant.now();
    }

    public void register(String role, String instanceId) {
        this.workerRole = role;
        this.instanceId = instanceId;
        this.status = "IDLE";
        this.activeTaskId = null;
        this.lastHeartbeatAt = Instant.now();
    }

    public void markOffline(String instanceId) {
        if (this.instanceId != null && !this.instanceId.equals(instanceId)) return;
        this.status = "OFFLINE";
        this.activeTaskId = null;
        this.lastHeartbeatAt = Instant.now();
    }

    public String getWorkerId() { return workerId; }
    public String getStatus() { return status; }
    public UUID getActiveTaskId() { return activeTaskId; }
    public String getMetadataJson() { return metadataJson; }
    public String getWorkerRole() { return workerRole; }
    public String getInstanceId() { return instanceId; }
    public Instant getLastHeartbeatAt() { return lastHeartbeatAt; }
}
