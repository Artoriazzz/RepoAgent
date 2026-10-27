package com.artoria.repoagentbackend.memory;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;

public record MemoryMergeBatchClaimRequest(
        @NotBlank String workerId,
        @Min(10) @Max(900) int leaseSeconds
) {
}
