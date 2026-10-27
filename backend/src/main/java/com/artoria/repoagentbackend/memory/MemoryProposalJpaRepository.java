package com.artoria.repoagentbackend.memory;

import jakarta.persistence.LockModeType;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Collection;
import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

public interface MemoryProposalJpaRepository extends JpaRepository<MemoryProposal, UUID> {
    Optional<MemoryProposal> findByIdempotencyKey(String idempotencyKey);

    @Query("""
            select proposal.memoryId from MemoryProposal proposal
            where proposal.status = :status
              and proposal.operation = :operation
              and proposal.memoryId is not null
              and proposal.createdAt <= :readyBefore
            group by proposal.memoryId
            order by min(proposal.createdAt) asc
            """)
    List<UUID> findPendingUpdateMemoryIds(
            @Param("status") MemoryProposalStatus status,
            @Param("operation") MemoryProposalOperation operation,
            @Param("readyBefore") Instant readyBefore,
            Pageable pageable
    );

    @Query("""
            select proposal.id from MemoryProposal proposal
            where proposal.status = :status
              and proposal.operation = :operation
              and proposal.createdAt <= :readyBefore
            order by proposal.createdAt asc
            """)
    List<UUID> findPendingCreateIds(
            @Param("status") MemoryProposalStatus status,
            @Param("operation") MemoryProposalOperation operation,
            @Param("readyBefore") Instant readyBefore,
            Pageable pageable
    );

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("""
            select proposal from MemoryProposal proposal
            where proposal.memoryId = :memoryId
              and proposal.baseVersion = :baseVersion
              and proposal.status = :status
              and proposal.operation = :operation
            order by proposal.createdAt asc
            """)
    List<MemoryProposal> findUpdatesForVersionForUpdate(
            @Param("memoryId") UUID memoryId,
            @Param("baseVersion") long baseVersion,
            @Param("status") MemoryProposalStatus status,
            @Param("operation") MemoryProposalOperation operation,
            Pageable pageable
    );

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("""
            select proposal from MemoryProposal proposal
            where proposal.memoryId = :memoryId
              and proposal.status = :status
              and proposal.operation = :operation
            order by proposal.createdAt asc
            """)
    List<MemoryProposal> findPendingUpdatesForMemoryForUpdate(
            @Param("memoryId") UUID memoryId,
            @Param("status") MemoryProposalStatus status,
            @Param("operation") MemoryProposalOperation operation
    );

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select proposal from MemoryProposal proposal where proposal.id = :id")
    Optional<MemoryProposal> findByIdForUpdate(@Param("id") UUID id);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("""
            select proposal from MemoryProposal proposal
            where proposal.mergeBatchId = :batchId and proposal.status = :status
            order by proposal.createdAt asc
            """)
    List<MemoryProposal> findByBatchIdForUpdate(
            @Param("batchId") UUID batchId,
            @Param("status") MemoryProposalStatus status
    );
}
