package com.artoria.repoagentbackend.memory;

import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Optional;
import java.util.UUID;

public interface MemoryRecallJpaRepository extends JpaRepository<MemoryRecall, UUID> {
    Optional<MemoryRecall> findByTaskIdAndMemoryIdAndMemoryRevision(
            UUID taskId, UUID memoryId, long memoryRevision
    );

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("""
            select recall from MemoryRecall recall
            where recall.taskId = :taskId and recall.memoryId = :memoryId
              and recall.memoryRevision = :revision
            """)
    Optional<MemoryRecall> findForFeedback(
            @Param("taskId") UUID taskId,
            @Param("memoryId") UUID memoryId,
            @Param("revision") long revision
    );

    @Modifying
    @Query(value = """
            INSERT IGNORE INTO memory_recall
                (id, task_id, memory_id, memory_revision, recall_score, created_at)
            VALUES (:id, :taskId, :memoryId, :revision, :score, CURRENT_TIMESTAMP(6))
            """, nativeQuery = true)
    int insertIfAbsent(
            @Param("id") String id,
            @Param("taskId") String taskId,
            @Param("memoryId") String memoryId,
            @Param("revision") long revision,
            @Param("score") double score
    );
}
