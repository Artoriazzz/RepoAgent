package com.artoria.repoagentbackend.repair;

import java.util.List;

public record BatchRepairTaskResponse(
        int requested,
        int accepted,
        int rejected,
        List<BatchRepairTaskItemResponse> items
) {
}
