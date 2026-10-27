package com.artoria.repoagentbackend.memory;

import jakarta.persistence.LockModeType;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Collection;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

public interface SemanticMemoryJpaRepository extends JpaRepository<SemanticMemory, UUID> {
    Optional<SemanticMemory> findByScopeKeyAndFingerprint(String scopeKey, String fingerprint);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select memory from SemanticMemory memory where memory.id = :id")
    Optional<SemanticMemory> findByIdForUpdate(@Param("id") UUID id);

    @Query("""
            select memory from SemanticMemory memory
            where memory.scopeKey in :scopeKeys
              and memory.status = :status
            order by memory.qualityScore desc, memory.updatedAt desc
            """)
    List<SemanticMemory> findRecallCandidates(
            @Param("scopeKeys") Collection<String> scopeKeys,
            @Param("status") MemoryStatus status,
            Pageable pageable
    );

    @Query("""
            select memory from SemanticMemory memory
            where memory.status = :status
              and (
                  memory.embeddingJson is null
                  or memory.embeddingProfile is null
                  or memory.embeddingProfile <> :profile
                  or memory.embeddingRevision is null
                  or memory.embeddingRevision <> memory.revision
              )
            order by memory.updatedAt asc, memory.id asc
            """)
    List<SemanticMemory> findNeedingEmbeddingProfile(
            @Param("status") MemoryStatus status,
            @Param("profile") String profile,
            Pageable pageable
    );

    @Query("""
            select memory from SemanticMemory memory
            where memory.status = :status
              and memory.embeddingJson is not null
              and memory.embeddingProfile = :profile
              and memory.embeddingRevision = memory.revision
            order by memory.updatedAt asc, memory.id asc
            """)
    List<SemanticMemory> findWithCurrentEmbedding(
            @Param("status") MemoryStatus status,
            @Param("profile") String profile,
            Pageable pageable
    );
}
