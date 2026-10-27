package com.artoria.repoagentbackend.repair;

public enum RepairTaskEventType {
    TASK_CREATED,
    STATUS_CHANGED,
    PROGRESS,
    ERROR,
    CANCEL_REQUESTED,
    LEASE_CLAIMED,
    LEASE_RENEWED,
    LEASE_EXPIRED,
    RETRY_CREATED
}
