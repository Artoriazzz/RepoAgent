package com.artoria.repoagentbackend.issue;

import com.artoria.repoagentbackend.common.ConflictException;
import com.artoria.repoagentbackend.common.NotFoundException;
import com.artoria.repoagentbackend.repository.CodeRepository;
import com.artoria.repoagentbackend.repository.CodeRepositoryService;
import com.artoria.repoagentbackend.repository.RepositoryAccessService;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import com.artoria.repoagentbackend.repository.RepositoryProvider;
import com.artoria.repoagentbackend.repair.BenchmarkEvaluationStatus;
import com.artoria.repoagentbackend.repair.RepairTaskStatus;
import java.util.Set;

import java.util.UUID;

@Service
public class RepositoryIssueService {
    private final RepositoryIssueJpaRepository issues;
    private final CodeRepositoryService repositoryService;
    private final RepositoryAccessService accessService;

    public RepositoryIssueService(
            RepositoryIssueJpaRepository issues,
            CodeRepositoryService repositoryService,
            RepositoryAccessService accessService
    ) {
        this.issues = issues;
        this.repositoryService = repositoryService;
        this.accessService = accessService;
    }

    @Transactional
    public IssueResponse create(UUID repositoryId, UUID actorId, CreateIssueRequest request) {
        accessService.requireMember(repositoryId, actorId);
        CodeRepository repository = repositoryService.getRequired(repositoryId);
        if (issues.existsByRepositoryIdAndGithubIssueNumber(repositoryId, request.githubIssueNumber())) {
            throw new ConflictException("Issue number is already recorded for this repository");
        }
        RepositoryIssue issue = issues.save(new RepositoryIssue(
                repository,
                request.githubIssueNumber(),
                request.title(),
                request.body() == null ? "" : request.body()
        ));
        return IssueResponse.from(issue);
    }

    @Transactional(readOnly = true)
    public RepositoryIssue getRequired(UUID id) {
        return issues.findById(id)
                .orElseThrow(() -> new NotFoundException("Issue not found: " + id));
    }

    @Transactional
    public RepositoryIssue getRequiredForUpdate(UUID id) {
        return issues.findByIdForUpdate(id)
                .orElseThrow(() -> new NotFoundException("Issue not found: " + id));
    }

    @Transactional(readOnly = true)
    public IssueResponse get(UUID issueId, UUID actorId) {
        RepositoryIssue issue = getRequired(issueId);
        accessService.requireMember(issue.getRepository().getId(), actorId);
        return IssueResponse.from(issue);
    }

    @Transactional(readOnly = true)
    public Page<IssueResponse> list(UUID repositoryId, UUID actorId, Pageable pageable) {
        repositoryService.getRequired(repositoryId);
        accessService.requireMember(repositoryId, actorId);
        return issues.findByRepositoryId(repositoryId, pageable).map(IssueResponse::from);
    }

    @Transactional(readOnly = true)
    public Page<IssueResponse> listAccessible(
            UUID actorId, IssueState state, RepositoryProvider provider,
            boolean repairable, Pageable pageable
    ) {
        Set<RepairTaskStatus> activeStatuses = Set.of(
                RepairTaskStatus.CREATED,
                RepairTaskStatus.QUEUED,
                RepairTaskStatus.ANALYZING,
                RepairTaskStatus.FIXING,
                RepairTaskStatus.VALIDATING
        );
        return issues.findAccessible(
                actorId, state, provider, repairable, activeStatuses,
                BenchmarkEvaluationStatus.RUNNING, BenchmarkEvaluationStatus.RESOLVED, pageable
        ).map(IssueResponse::from);
    }

    @Transactional
    public RepositoryIssue findOrCreateSweBench(
            CodeRepository repository,
            String instanceId,
            String title,
            String body,
            String datasetSplit,
            String baseSha
    ) {
        return issues.findByRepositoryIdAndExternalId(repository.getId(), instanceId)
                .map(issue -> {
                    issue.synchronizeSweBench(title, body, datasetSplit, baseSha);
                    return issue;
                })
                .orElseGet(() -> issues.save(RepositoryIssue.sweBench(
                        repository, instanceId, title, body, datasetSplit, baseSha
                )));
    }

    @Transactional
    public void synchronizeFromGitHub(
            CodeRepository repository,
            int githubIssueNumber,
            String title,
            String body,
            IssueState state
    ) {
        issues.findByRepositoryIdAndGithubIssueNumber(repository.getId(), githubIssueNumber)
                .ifPresentOrElse(
                        issue -> issue.synchronize(title, body, state),
                        () -> {
                            RepositoryIssue issue = new RepositoryIssue(repository, githubIssueNumber, title, body);
                            issue.synchronize(title, body, state);
                            issues.save(issue);
                        }
                );
    }
}
