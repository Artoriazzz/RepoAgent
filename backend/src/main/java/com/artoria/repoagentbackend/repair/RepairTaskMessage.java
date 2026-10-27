package com.artoria.repoagentbackend.repair;

import java.util.UUID;
import java.io.Serializable;

/**
 * Contract published to the future RepoAgent Worker. The worker loads the remaining
 * issue and repository snapshot through an authenticated internal API rather than
 * copying large source or issue bodies into RabbitMQ.
 */
public record RepairTaskMessage(
        UUID messageId,
        UUID taskId,
        UUID issueId,
        String baseSha,
        String branchName,
        RepairPipelineStage stage,
        long stageSequence
) implements Serializable {
    /**
     * 兼容旧的四字段消息生产者；新消息始终显式携带阶段。
     * 这避免滚动发布时旧进程因 record 字段扩展而直接失效。
     */
    public RepairTaskMessage(UUID taskId, UUID issueId, String baseSha, String branchName) {
        this(UUID.randomUUID(), taskId, issueId, baseSha, branchName, RepairPipelineStage.COORDINATOR, 0);
    }

    public RepairTaskMessage(UUID taskId, UUID issueId, String baseSha, String branchName, RepairPipelineStage stage) {
        this(UUID.randomUUID(), taskId, issueId, baseSha, branchName, stage, 0);
    }

    public RepairPipelineStage effectiveStage() {
        return stage == null ? RepairPipelineStage.COORDINATOR : stage;
    }
}
