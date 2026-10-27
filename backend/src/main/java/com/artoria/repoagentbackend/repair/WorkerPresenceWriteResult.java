package com.artoria.repoagentbackend.repair;

/** Redis 心跳写入结果。registrationRequired 用于 Redis 重启或 TTL 到期后的当前实例重新登记。 */
public record WorkerPresenceWriteResult(boolean available, boolean accepted, boolean registrationRequired) {
    public static WorkerPresenceWriteResult acceptedResult() {
        return new WorkerPresenceWriteResult(true, true, false);
    }

    public static WorkerPresenceWriteResult rejectedResult() {
        return new WorkerPresenceWriteResult(true, false, false);
    }

    public static WorkerPresenceWriteResult registrationRequiredResult() {
        return new WorkerPresenceWriteResult(true, false, true);
    }

    public static WorkerPresenceWriteResult unavailableResult() {
        return new WorkerPresenceWriteResult(false, false, true);
    }
}
