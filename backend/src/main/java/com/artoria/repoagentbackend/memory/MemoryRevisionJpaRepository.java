package com.artoria.repoagentbackend.memory;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;
import java.util.UUID;

public interface MemoryRevisionJpaRepository extends JpaRepository<MemoryRevision, UUID> {
    List<MemoryRevision> findByMemoryIdOrderByRevisionNumberDesc(UUID memoryId);

    @Query("""
            select distinct revision.memoryId
            from MemoryRevision revision, MemoryObservation observation, RepairTask task
            where revision.sourceObservationId = observation.id
              and observation.taskId = task.id
              and task.issue.id = :issueId
            """)
    List<UUID> findMemoryIdsSourcedFromIssue(@Param("issueId") UUID issueId);
}
