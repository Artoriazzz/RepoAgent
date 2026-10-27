package com.artoria.repoagentbackend.repair;

import java.util.EnumSet;
import java.util.Set;

public enum RepairTaskStatus {
    CREATED,
    QUEUED,
    ANALYZING,
    FIXING,
    VALIDATING,
    PATCH_READY,
    PR_READY,
    FAILED,
    CANCELLED;

    public boolean canTransitionTo(RepairTaskStatus target) {
        return allowedTargets().contains(target);
    }

    private Set<RepairTaskStatus> allowedTargets() {
        return switch (this) {
            case CREATED -> EnumSet.of(QUEUED, CANCELLED);
            case QUEUED -> EnumSet.of(ANALYZING, FIXING, VALIDATING, CANCELLED, FAILED);
            // 阶段完成后会先回到下一个角色的持久化队列，真正被领取时再进入执行态。
            case ANALYZING -> EnumSet.of(QUEUED, FIXING, FAILED, CANCELLED);
            case FIXING -> EnumSet.of(QUEUED, VALIDATING, FAILED, CANCELLED);
            // A failed validation may enter the one allowed corrective pass.
            case VALIDATING -> EnumSet.of(QUEUED, FIXING, PATCH_READY, FAILED, CANCELLED);
            case PATCH_READY -> EnumSet.of(PR_READY, FAILED, CANCELLED);
            case PR_READY, FAILED, CANCELLED -> EnumSet.noneOf(RepairTaskStatus.class);
        };
    }
}
