package com.artoria.repoagentbackend.benchmark;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public record CreateSweBenchRepairRequest(
        @NotBlank
        @Size(max = 255)
        @Pattern(regexp = "[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+")
        String instanceId,

        @NotBlank
        @Size(max = 255)
        @Pattern(regexp = "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
        String repository,

        @NotBlank
        @Pattern(regexp = "[0-9a-fA-F]{7,128}", message = "must be a Git commit SHA")
        String baseCommit,

        @NotBlank
        @Size(max = 200_000)
        String problemStatement,

        @NotBlank
        @Size(max = 64)
        String datasetSplit,

        @Size(max = 255)
        String runKey
) {
}
