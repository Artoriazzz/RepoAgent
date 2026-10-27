package com.artoria.repoagentbackend.repair;

import jakarta.persistence.LockModeType;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.Page;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.time.Instant;
import java.util.Collection;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

public interface RepairOutboxJpaRepository extends JpaRepository<RepairOutboxEvent, UUID> {
    Optional<RepairOutboxEvent> findByAggregateIdAndEventType(UUID aggregateId, String eventType);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select event from RepairOutboxEvent event where event.id = :id")
    Optional<RepairOutboxEvent> findByIdForUpdate(@Param("id") UUID id);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("""
            select event from RepairOutboxEvent event
            where event.status in :statuses and event.nextAttemptAt <= :now
            order by event.createdAt asc
            """)
    List<RepairOutboxEvent> findDispatchable(
            @Param("statuses") Collection<RepairOutboxStatus> statuses,
            @Param("now") Instant now,
            Pageable pageable
    );

    long countByStatusIn(Collection<RepairOutboxStatus> statuses);

    Page<RepairOutboxEvent> findByStatus(RepairOutboxStatus status, Pageable pageable);

    long countByStatus(RepairOutboxStatus status);
}
