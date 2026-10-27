package com.artoria.repoagentbackend.memory;

import jakarta.persistence.LockModeType;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.time.Instant;
import java.util.Collection;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

public interface MemoryIndexJobJpaRepository extends JpaRepository<MemoryIndexJob, UUID> {
    Optional<MemoryIndexJob> findByMemoryIdAndMemoryRevisionAndEmbeddingProfile(
            UUID memoryId, long memoryRevision, String embeddingProfile
    );

    @Query("""
            select job.id from MemoryIndexJob job
            where job.status in :statuses and job.nextAttemptAt <= :now
            order by job.createdAt asc
            """)
    List<UUID> findDispatchableIds(
            @Param("statuses") Collection<MemoryIndexJobStatus> statuses,
            @Param("now") Instant now,
            Pageable pageable
    );

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select job from MemoryIndexJob job where job.id = :id")
    Optional<MemoryIndexJob> findByIdForUpdate(@Param("id") UUID id);

    @Modifying
    @Query("""
            update MemoryIndexJob job
            set job.status = :failed, job.nextAttemptAt = :now, job.claimedAt = null,
                job.lastError = :reason, job.updatedAt = :now
            where job.status = :processing and job.claimedAt < :cutoff
            """)
    int recoverStale(
            @Param("processing") MemoryIndexJobStatus processing,
            @Param("failed") MemoryIndexJobStatus failed,
            @Param("cutoff") Instant cutoff,
            @Param("now") Instant now,
            @Param("reason") String reason
    );
}
