package com.artoria.repoagentbackend.repair;

import java.util.Map;

/** Redis 可用性与当前有效 Worker 的一次读取结果。 */
public record WorkerPresenceSnapshot(boolean available, Map<String, WorkerPresence> workers) {
    public WorkerPresenceSnapshot {
        workers = Map.copyOf(workers);
    }

    public static WorkerPresenceSnapshot unavailable() {
        return new WorkerPresenceSnapshot(false, Map.of());
    }
}
