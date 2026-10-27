package com.artoria.repoagentbackend.repair;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.PositiveOrZero;
import jakarta.validation.constraints.Size;

import java.time.Instant;
import java.util.UUID;

public record WorkerHeartbeatRequest(
        @NotBlank @Size(max = 32) String status,
        UUID activeTaskId,
        @Size(max = 20_000) String metadataJson,
        @Size(max = 128) String instanceId,
        @PositiveOrZero Long sequence,
        @Size(max = 64) String role,
        Instant startedAt
) {
}
