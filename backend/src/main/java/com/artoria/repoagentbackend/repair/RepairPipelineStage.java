package com.artoria.repoagentbackend.repair;

import java.util.EnumSet;
import java.util.Set;

/**
 * 一个修复任务在外层 Worker 池中的持久化阶段。
 *
 * 这里描述的是跨进程、跨队列的编排阶段，不替代 RepoAgent 内部更细的工具调用轨迹。
 */
public enum RepairPipelineStage {
    COORDINATOR("coordinator", "repoagent.repair.stage.coordinator", "stage.coordinator", "COORDINATOR"),
    IMPLEMENT("implement", "repoagent.repair.stage.implement", "stage.implement", "IMPLEMENT"),
    VALIDATION("validation", "repoagent.repair.stage.validation", "stage.validation", "VALIDATING"),
    FAILURE_REVIEW("failure-review", "repoagent.repair.stage.failure-review", "stage.failure-review", "RETRYING"),
    CORRECTIVE_IMPLEMENT(
            "corrective-implement",
            "repoagent.repair.stage.corrective-implement",
            "stage.corrective-implement",
            "CORRECTIVE_IMPLEMENT"
    ),
    EXPERIENCE_MEMORY("experience-memory", "repoagent.repair.stage.experience-memory", "stage.experience-memory", "EXPERIENCE_MEMORY");

    private final String workerRole;
    private final String queueName;
    private final String routingKey;
    private final String displayStage;

    RepairPipelineStage(String workerRole, String queueName, String routingKey, String displayStage) {
        this.workerRole = workerRole;
        this.queueName = queueName;
        this.routingKey = routingKey;
        this.displayStage = displayStage;
    }

    public String workerRole() { return workerRole; }
    public String queueName() { return queueName; }
    public String routingKey() { return routingKey; }
    public String displayStage() { return displayStage; }

    public boolean canHandoffTo(RepairPipelineStage target) {
        return switch (this) {
            case COORDINATOR -> target == IMPLEMENT;
            case IMPLEMENT, CORRECTIVE_IMPLEMENT -> target == VALIDATION;
            case VALIDATION -> target == FAILURE_REVIEW || target == EXPERIENCE_MEMORY;
            case FAILURE_REVIEW -> target == CORRECTIVE_IMPLEMENT || target == EXPERIENCE_MEMORY;
            case EXPERIENCE_MEMORY -> false;
        };
    }

    public static Set<RepairPipelineStage> all() {
        return EnumSet.allOf(RepairPipelineStage.class);
    }
}
