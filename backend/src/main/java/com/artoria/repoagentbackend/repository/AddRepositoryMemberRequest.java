package com.artoria.repoagentbackend.repository;

import jakarta.validation.constraints.NotNull;

import java.util.UUID;

public record AddRepositoryMemberRequest(@NotNull UUID userId, @NotNull RepositoryRole role) {
}

