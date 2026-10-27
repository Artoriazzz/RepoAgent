package com.artoria.repoagentbackend.memory;

import java.util.UUID;

public record MemoryProposalResponse(
        UUID proposalId,
        MemoryProposalStatus status,
        UUID memoryId,
        Long appliedRevision,
        String conflictReason,
        MemoryResponse currentMemory
) {
    static MemoryProposalResponse from(MemoryProposal proposal, SemanticMemory memory) {
        return new MemoryProposalResponse(
                proposal.getId(), proposal.getStatus(), proposal.getMemoryId(),
                proposal.getAppliedRevision(), proposal.getConflictReason(),
                memory == null ? null : MemoryResponse.from(memory)
        );
    }
}
