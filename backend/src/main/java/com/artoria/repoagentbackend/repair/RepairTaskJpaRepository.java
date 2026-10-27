package com.artoria.repoagentbackend.repair;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.EntityGraph;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.data.jpa.repository.Lock;

import jakarta.persistence.LockModeType;
import java.time.Instant;
import java.util.Collection;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

public interface RepairTaskJpaRepository extends JpaRepository<RepairTask, UUID> {
    Optional<RepairTask> findByIssueIdAndIdempotencyKey(UUID issueId, String idempotencyKey);
    Optional<RepairTask> findBySubmittedByUserIdAndIdempotencyKey(UUID userId, String idempotencyKey);
    long countByWorkflowStatusAndStageStatus(RepairWorkflowStatus workflowStatus, RepairStageStatus stageStatus);
    Optional<RepairTask> findFirstByIssueIdOrderByCreatedAtDesc(UUID issueId);

    @Query("""
            select count(task) > 0 from RepairTask task
            where task.issue.id = :issueId
              and (task.status in :activeStatuses
                   or task.evaluationStatus in :blockingEvaluationStatuses)
            """)
    boolean existsBlockingRepair(
            @Param("issueId") UUID issueId,
            @Param("activeStatuses") Collection<RepairTaskStatus> activeStatuses,
            @Param("blockingEvaluationStatuses") Collection<BenchmarkEvaluationStatus> blockingEvaluationStatuses
    );

    @EntityGraph(attributePaths = {"issue", "issue.repository"})
    Optional<RepairTask> findWithIssueById(UUID id);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select task from RepairTask task where task.id = :id")
    Optional<RepairTask> findByIdForUpdate(@Param("id") UUID id);

    @Query("""
            select task from RepairTask task
            where exists (
                select member.id from RepositoryMember member
                where member.repository = task.issue.repository and member.user.id = :userId
            )
            and (:repositoryId is null or task.issue.repository.id = :repositoryId)
            and (:status is null or task.status = :status)
            and (:runKey is null or task.runKey = :runKey)
            """)
    Page<RepairTask> findAccessible(
            @Param("userId") UUID userId,
            @Param("repositoryId") UUID repositoryId,
            @Param("status") RepairTaskStatus status,
            @Param("runKey") String runKey,
            Pageable pageable
    );

    @Query("""
            select task from RepairTask task
            where exists (
                select member.id from RepositoryMember member
                where member.repository = task.issue.repository and member.user.id = :userId
            )
            and (:repositoryId is null or task.issue.repository.id = :repositoryId)
            and (:runKey is null or task.runKey = :runKey)
            """)
    List<RepairTask> findAccessibleForStatistics(
            @Param("userId") UUID userId,
            @Param("repositoryId") UUID repositoryId,
            @Param("runKey") String runKey
    );

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("""
            select task from RepairTask task
            where task.status in :statuses
              and task.workerId is not null
              and task.leaseExpiresAt < :now
            order by task.leaseExpiresAt asc
            """)
    List<RepairTask> findExpiredLeases(
            @Param("statuses") Collection<RepairTaskStatus> statuses,
            @Param("now") Instant now,
            Pageable pageable
    );

    /**
     * 兼容旧版流水线：旧实现会在交接后留下无租约的 ANALYZING/FIXING/VALIDATING
     * 任务。新版交接一律使用 QUEUED，因此这些记录可以安全地重新投递。
     */
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("""
            select task from RepairTask task
            where task.status in :statuses
              and task.workerId is null
              and task.updatedAt < :cutoff
            order by task.updatedAt asc
            """)
    List<RepairTask> findStrandedUnleasedPipelineTasks(
            @Param("statuses") Collection<RepairTaskStatus> statuses,
            @Param("cutoff") Instant cutoff,
            Pageable pageable
    );
}
