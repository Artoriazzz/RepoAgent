package com.artoria.repoagentbackend.repository;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Positive;

public record CreateCodeRepositoryRequest(
        @Positive long githubRepositoryId,
        @NotBlank String ownerLogin,
        @NotBlank String name,
        @NotBlank String defaultBranch
) {
}
