package com.artoria.repoagentbackend.memory;

import jakarta.persistence.LockModeType;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

public interface MemoryMergeBatchJpaRepository extends JpaRepository<MemoryMergeBatch, UUID> {
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select batch from MemoryMergeBatch batch where batch.id = :id")
    Optional<MemoryMergeBatch> findByIdForUpdate(@Param("id") UUID id);

    @Query("""
            select batch.id from MemoryMergeBatch batch
            where batch.status = :status and batch.leaseExpiresAt < :now
            order by batch.leaseExpiresAt asc
            """)
    List<UUID> findExpiredIds(
            @Param("status") MemoryMergeBatchStatus status,
            @Param("now") Instant now,
            Pageable pageable
    );

    @Query("""
            select count(batch) > 0 from MemoryMergeBatch batch
            where batch.memoryId = :memoryId
              and batch.baseVersion = :baseVersion
              and batch.status = :status
            """)
    boolean existsRunningForMemoryVersion(
            @Param("memoryId") UUID memoryId,
            @Param("baseVersion") long baseVersion,
            @Param("status") MemoryMergeBatchStatus status
    );

    /** CREATE 没有现成 memoryId；同一作用域只允许一个创建批次，以避免唯一 fingerprint 并发冲突。 */
    @Query("""
            select count(batch) > 0 from MemoryMergeBatch batch
            where batch.operation = :operation
              and batch.scopeType = :scopeType
              and batch.status = :status
              and ((:repositoryId is null and batch.repositoryId is null) or batch.repositoryId = :repositoryId)
            """)
    boolean existsRunningCreateForScope(
            @Param("operation") MemoryProposalOperation operation,
            @Param("scopeType") MemoryScopeType scopeType,
            @Param("repositoryId") UUID repositoryId,
            @Param("status") MemoryMergeBatchStatus status
    );
}
