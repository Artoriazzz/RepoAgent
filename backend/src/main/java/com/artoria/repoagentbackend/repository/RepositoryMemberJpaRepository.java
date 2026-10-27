package com.artoria.repoagentbackend.repository;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;
import java.util.UUID;

public interface RepositoryMemberJpaRepository extends JpaRepository<RepositoryMember, UUID> {
    Optional<RepositoryMember> findByRepositoryIdAndUserId(UUID repositoryId, UUID userId);
}

