package com.artoria.repoagentbackend.identity;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Positive;

public record CreateUserRequest(
        @Positive long githubUserId,
        @NotBlank String login,
        @NotBlank String displayName
) {
}

