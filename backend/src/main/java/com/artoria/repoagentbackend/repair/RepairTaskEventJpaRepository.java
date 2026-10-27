package com.artoria.repoagentbackend.repair;

import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Collection;
import java.util.List;
import java.util.UUID;
import java.util.Optional;
import java.time.Instant;

public interface RepairTaskEventJpaRepository extends JpaRepository<RepairTaskEvent, UUID> {
    Page<RepairTaskEvent> findByTaskIdOrderByCreatedAtAsc(UUID taskId, Pageable pageable);
    Page<RepairTaskEvent> findByTaskIdAndCreatedAtAfterOrderByCreatedAtAsc(
            UUID taskId, Instant after, Pageable pageable
    );
    Optional<RepairTaskEvent> findFirstByTaskIdOrderByCreatedAtDesc(UUID taskId);

    @Query("""
            select event.task.id, min(event.createdAt)
            from RepairTaskEvent event
            where event.task.id in :taskIds and event.eventType = :eventType
            group by event.task.id
            """)
    List<Object[]> findFirstEventTimes(
            @Param("taskIds") Collection<UUID> taskIds,
            @Param("eventType") RepairTaskEventType eventType
    );
}
