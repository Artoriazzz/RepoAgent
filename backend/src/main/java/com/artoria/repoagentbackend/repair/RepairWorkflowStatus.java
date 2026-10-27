package com.artoria.repoagentbackend.repair;

/** 完整修复工作流的业务结果；不再复用 Agent 阶段状态。 */
public enum RepairWorkflowStatus {
    RUNNING,
    SUCCEEDED,
    FAILED,
    CANCELLED;

    public boolean isTerminal() {
        return this != RUNNING;
    }
}
