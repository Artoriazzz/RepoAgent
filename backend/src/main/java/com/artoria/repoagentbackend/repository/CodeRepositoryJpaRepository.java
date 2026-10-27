package com.artoria.repoagentbackend.repository;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.UUID;
import java.util.Optional;

public interface CodeRepositoryJpaRepository extends JpaRepository<CodeRepository, UUID> {
    boolean existsByGithubRepositoryId(long githubRepositoryId);
    Optional<CodeRepository> findByGithubRepositoryId(long githubRepositoryId);
    Optional<CodeRepository> findByProviderAndExternalId(RepositoryProvider provider, String externalId);

    @Query("""
            select repository from CodeRepository repository
            where exists (
                select member.id from RepositoryMember member
                where member.repository = repository and member.user.id = :userId
            )
            """)
    Page<CodeRepository> findAccessibleByUserId(@Param("userId") UUID userId, Pageable pageable);
}
