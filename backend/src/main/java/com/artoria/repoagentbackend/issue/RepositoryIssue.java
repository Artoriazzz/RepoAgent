package com.artoria.repoagentbackend.issue;

import com.artoria.repoagentbackend.repository.CodeRepository;
import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.FetchType;
import jakarta.persistence.Id;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;
import jakarta.persistence.Table;
import org.hibernate.annotations.CreationTimestamp;
import org.hibernate.annotations.UpdateTimestamp;
import org.hibernate.annotations.UuidGenerator;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "repository_issue")
public class RepositoryIssue {

    @Id
    @UuidGenerator
    private UUID id;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "repository_id", nullable = false)
    private CodeRepository repository;

    @Column(name = "github_issue_number")
    private Integer githubIssueNumber;

    @Column(name = "external_id")
    private String externalId;

    @Column(name = "benchmark_dataset_split")
    private String benchmarkDatasetSplit;

    @Column(name = "base_sha", length = 128)
    private String baseSha;

    @Column(nullable = false)
    private String title;

    @Column(nullable = false, columnDefinition = "LONGTEXT")
    private String body;

    @Enumerated(EnumType.STRING)
    @Column(name = "issue_state", nullable = false)
    private IssueState state;

    @CreationTimestamp
    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @UpdateTimestamp
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected RepositoryIssue() {
    }

    public RepositoryIssue(CodeRepository repository, int githubIssueNumber, String title, String body) {
        this.repository = repository;
        this.githubIssueNumber = githubIssueNumber;
        this.title = title;
        this.body = body;
        this.state = IssueState.OPEN;
    }

    public static RepositoryIssue sweBench(
            CodeRepository repository,
            String instanceId,
            String title,
            String body,
            String datasetSplit,
            String baseSha
    ) {
        RepositoryIssue issue = new RepositoryIssue();
        issue.repository = repository;
        issue.externalId = instanceId;
        issue.title = title;
        issue.body = body;
        issue.benchmarkDatasetSplit = datasetSplit;
        issue.baseSha = baseSha;
        issue.state = IssueState.OPEN;
        return issue;
    }

    public UUID getId() { return id; }
    public CodeRepository getRepository() { return repository; }
    public Integer getGithubIssueNumber() { return githubIssueNumber; }
    public String getExternalId() { return externalId; }
    public String getBenchmarkDatasetSplit() { return benchmarkDatasetSplit; }
    public String getBaseSha() { return baseSha; }
    public String getTitle() { return title; }
    public String getBody() { return body; }
    public IssueState getState() { return state; }

    public void synchronize(String title, String body, IssueState state) {
        this.title = title;
        this.body = body;
        this.state = state;
    }

    public void synchronizeSweBench(String title, String body, String datasetSplit, String baseSha) {
        synchronize(title, body, IssueState.OPEN);
        this.benchmarkDatasetSplit = datasetSplit;
        this.baseSha = baseSha;
    }
}
