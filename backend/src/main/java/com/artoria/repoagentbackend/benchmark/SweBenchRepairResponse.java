package com.artoria.repoagentbackend.benchmark;

import com.artoria.repoagentbackend.repair.RepairTaskResponse;

import java.util.UUID;

public record SweBenchRepairResponse(
        String instanceId,
        String repository,
        String datasetSplit,
        UUID repositoryId,
        UUID issueId,
        RepairTaskResponse task
) {
}
