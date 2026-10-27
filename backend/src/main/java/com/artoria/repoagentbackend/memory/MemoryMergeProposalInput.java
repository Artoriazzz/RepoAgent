package com.artoria.repoagentbackend.memory;

import java.util.UUID;

/** 提供给 Memory Agent 的固定批次输入，不包含完整 patch 或隐藏测试。 */
public record MemoryMergeProposalInput(
        UUID proposalId,
        UUID observationId,
        String changeReason,
        MemoryContent proposedContent,
        String problemSignature,
        String patchSummary,
        String validationSummary
) {
}
