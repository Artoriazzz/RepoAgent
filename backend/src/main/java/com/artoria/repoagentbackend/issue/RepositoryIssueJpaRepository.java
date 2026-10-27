package com.artoria.repoagentbackend.issue;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.EntityGraph;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import jakarta.persistence.LockModeType;

import java.util.UUID;
import java.util.Optional;
import com.artoria.repoagentbackend.repository.RepositoryProvider;
import com.artoria.repoagentbackend.repair.BenchmarkEvaluationStatus;
import com.artoria.repoagentbackend.repair.RepairTaskStatus;
import java.util.Collection;

public interface RepositoryIssueJpaRepository extends JpaRepository<RepositoryIssue, UUID> {
    boolean existsByRepositoryIdAndGithubIssueNumber(UUID repositoryId, int githubIssueNumber);
    Optional<RepositoryIssue> findByRepositoryIdAndGithubIssueNumber(UUID repositoryId, int githubIssueNumber);
    Optional<RepositoryIssue> findByRepositoryIdAndExternalId(UUID repositoryId, String externalId);
    Page<RepositoryIssue> findByRepositoryId(UUID repositoryId, Pageable pageable);

    @EntityGraph(attributePaths = "repository")
    @Query("""
            select issue from RepositoryIssue issue
            where exists (
                select member.id from RepositoryMember member
                where member.repository = issue.repository and member.user.id = :userId
            )
            and (:state is null or issue.state = :state)
            and (:provider is null or issue.repository.provider = :provider)
            and (:repairable = false or (
                issue.baseSha is not null
                and not exists (
                    select activeTask.id from RepairTask activeTask
                    where activeTask.issue = issue
                      and (activeTask.status in :activeStatuses
                           or activeTask.evaluationStatus = :runningStatus)
                )
                and not exists (
                    select resolvedTask.id from RepairTask resolvedTask
                    where resolvedTask.issue = issue
                      and resolvedTask.evaluationStatus = :resolvedStatus
                )
            ))
            """)
    Page<RepositoryIssue> findAccessible(
            @Param("userId") UUID userId,
            @Param("state") IssueState state,
            @Param("provider") RepositoryProvider provider,
            @Param("repairable") boolean repairable,
            @Param("activeStatuses") Collection<RepairTaskStatus> activeStatuses,
            @Param("runningStatus") BenchmarkEvaluationStatus runningStatus,
            @Param("resolvedStatus") BenchmarkEvaluationStatus resolvedStatus,
            Pageable pageable
    );

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select issue from RepositoryIssue issue where issue.id = :id")
    Optional<RepositoryIssue> findByIdForUpdate(@Param("id") UUID id);
}
