package com.artoria.repoagentbackend.repository;

import java.util.UUID;

public record CodeRepositoryResponse(
        UUID id,
        Long githubRepositoryId,
        RepositoryProvider provider,
        String externalId,
        String ownerLogin,
        String name,
        String defaultBranch,
        UUID ownerUserId
) {
    static CodeRepositoryResponse from(CodeRepository repository) {
        return new CodeRepositoryResponse(
                repository.getId(),
                repository.getGithubRepositoryId(),
                repository.getProvider(),
                repository.getExternalId(),
                repository.getOwnerLogin(),
                repository.getName(),
                repository.getDefaultBranch(),
                repository.getOwner() == null ? null : repository.getOwner().getId()
        );
    }
}
