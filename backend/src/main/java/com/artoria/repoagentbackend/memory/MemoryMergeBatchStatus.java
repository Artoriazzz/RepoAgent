package com.artoria.repoagentbackend.memory;

public enum MemoryMergeBatchStatus {
    RUNNING,
    APPLIED,
    NO_CHANGE,
    STALE,
    FAILED
}
