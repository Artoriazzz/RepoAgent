package com.artoria.repoagentbackend.identity;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.UUID;

public interface AppUserJpaRepository extends JpaRepository<AppUser, UUID> {
    boolean existsByGithubUserId(long githubUserId);
    boolean existsByLogin(String login);
}

