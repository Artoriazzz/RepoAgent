package com.artoria.repoagentbackend.repair;

import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Size;

import java.util.List;
import java.util.UUID;

public record BatchCreateRepairTasksRequest(
        @NotEmpty @Size(max = 50) List<UUID> issueIds,
        @Size(max = 255) String runKey
) {
}
