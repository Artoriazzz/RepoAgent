package com.artoria.repoagentbackend.repair;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;

public record CreateRepairTaskRequest(
        @NotBlank
        @Pattern(regexp = "[0-9a-fA-F]{7,128}", message = "must be a Git commit SHA")
        String baseSha
) {
}
