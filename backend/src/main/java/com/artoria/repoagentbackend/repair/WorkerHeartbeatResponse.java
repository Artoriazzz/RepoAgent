package com.artoria.repoagentbackend.repair;

import java.time.Instant;
import java.util.UUID;

public record WorkerHeartbeatResponse(
        String workerId,
        String status,
        UUID activeTaskId,
        Instant lastHeartbeatAt,
        boolean accepted,
        boolean presenceAvailable,
        boolean registrationRequired
) {
    static WorkerHeartbeatResponse from(WorkerNode worker) {
        return new WorkerHeartbeatResponse(
                worker.getWorkerId(), worker.getStatus(), worker.getActiveTaskId(), worker.getLastHeartbeatAt(),
                true, true, false
        );
    }
}
