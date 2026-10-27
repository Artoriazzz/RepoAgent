package com.artoria.repoagentbackend.repair;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record WorkerRegistrationRequest(
        @NotBlank @Size(max = 128) String instanceId,
        @NotBlank @Size(max = 64) String role
) {
}
