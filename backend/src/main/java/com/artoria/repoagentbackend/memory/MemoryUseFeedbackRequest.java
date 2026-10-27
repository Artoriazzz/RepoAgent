package com.artoria.repoagentbackend.memory;

import jakarta.validation.constraints.NotNull;

import java.util.UUID;

public record MemoryUseFeedbackRequest(
        @NotNull UUID taskId,
        long revision,
        boolean used,
        boolean successful
) {
}
