package com.artoria.repoagentbackend.memory;

public enum MemoryProposalStatus {
    PENDING,
    PROCESSING,
    APPLIED,
    STALE,
    CONFLICT,
    REJECTED,
    NO_CHANGE
}
