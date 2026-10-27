package com.artoria.repoagentbackend.issue;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Positive;

public record CreateIssueRequest(
        @Positive int githubIssueNumber,
        @NotBlank String title,
        String body
) {
}
