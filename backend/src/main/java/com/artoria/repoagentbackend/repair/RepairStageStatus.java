package com.artoria.repoagentbackend.repair;

/** 当前 Agent 阶段在持久化工作流中的执行状态。 */
public enum RepairStageStatus {
    WAITING,
    RUNNING,
    SUCCEEDED,
    FAILED
}
