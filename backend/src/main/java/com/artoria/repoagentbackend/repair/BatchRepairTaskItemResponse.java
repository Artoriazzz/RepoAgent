package com.artoria.repoagentbackend.repair;

import java.util.UUID;

public record BatchRepairTaskItemResponse(
        UUID issueId,
        boolean accepted,
        RepairTaskResponse task,
        String error
) {
    static BatchRepairTaskItemResponse accepted(UUID issueId, RepairTaskResponse task) {
        return new BatchRepairTaskItemResponse(issueId, true, task, null);
    }

    static BatchRepairTaskItemResponse rejected(UUID issueId, String error) {
        return new BatchRepairTaskItemResponse(issueId, false, null, error);
    }
}
