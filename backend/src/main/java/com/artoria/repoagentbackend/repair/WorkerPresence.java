package com.artoria.repoagentbackend.repair;

import java.time.Instant;
import java.util.UUID;

/** Redis 中带 TTL 的 Worker 实时状态；MySQL 中的 WorkerNode 只保留注册历史。 */
public record WorkerPresence(
        String workerId,
        String instanceId,
        String role,
        String status,
        UUID activeTaskId,
        String metadataJson,
        Instant startedAt,
        Instant lastHeartbeatAt,
        long sequence
) {
}
