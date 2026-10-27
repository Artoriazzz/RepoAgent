package com.artoria.repoagentbackend.memory;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "semantic_memory")
public class SemanticMemory {
    @Id
    private UUID id;

    @Enumerated(EnumType.STRING)
    @Column(name = "scope_type", nullable = false)
    private MemoryScopeType scopeType;

    @Column(name = "scope_key", nullable = false)
    private String scopeKey;

    @Column(name = "repository_id")
    private UUID repositoryId;

    @Column(nullable = false)
    private String title;

    @Column(nullable = false, columnDefinition = "TEXT")
    private String summary;

    @Column(name = "retrieval_text", nullable = false, columnDefinition = "TEXT")
    private String retrievalText;

    @Column(nullable = false, columnDefinition = "LONGTEXT")
    private String applicability;

    @Column(name = "repair_strategy", nullable = false, columnDefinition = "LONGTEXT")
    private String repairStrategy;

    @Column(name = "verification_strategy", columnDefinition = "LONGTEXT")
    private String verificationStrategy;

    @Column(name = "anti_patterns", columnDefinition = "LONGTEXT")
    private String antiPatterns;

    @Column(name = "language_name")
    private String languageName;

    @Column(name = "framework_name")
    private String frameworkName;

    @Column(name = "problem_type")
    private String problemType;

    @Column(name = "dependency_range")
    private String dependencyRange;

    @Column(nullable = false)
    private String fingerprint;

    @Enumerated(EnumType.STRING)
    @Column(name = "memory_status", nullable = false)
    private MemoryStatus status;

    @Column(name = "quality_score", nullable = false)
    private double qualityScore;

    @Column(name = "successful_uses", nullable = false)
    private long successfulUses;

    @Column(name = "failed_uses", nullable = false)
    private long failedUses;

    @Column(name = "content_revision", nullable = false)
    private long revision;

    @Version
    @Column(name = "row_version", nullable = false)
    private long rowVersion;

    @Column(name = "embedding_json", columnDefinition = "LONGTEXT")
    private String embeddingJson;

    @Column(name = "embedding_model")
    private String embeddingModel;

    @Column(name = "embedding_profile")
    private String embeddingProfile;

    @Column(name = "embedding_dimensions")
    private Integer embeddingDimensions;

    @Column(name = "embedding_revision")
    private Long embeddingRevision;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected SemanticMemory() {
    }

    public SemanticMemory(MemoryScopeType scopeType, UUID repositoryId, String scopeKey,
                          String fingerprint, MemoryContent content) {
        this.id = UUID.randomUUID();
        this.scopeType = scopeType;
        this.repositoryId = repositoryId;
        this.scopeKey = scopeKey;
        this.fingerprint = fingerprint;
        this.status = MemoryStatus.ACTIVE;
        this.qualityScore = 0.5;
        this.revision = 1;
        this.createdAt = Instant.now();
        assign(content);
    }

    public void apply(MemoryContent content) {
        assign(content);
        revision++;
    }

    private void assign(MemoryContent content) {
        this.title = content.title();
        this.summary = content.summary();
        this.retrievalText = content.effectiveRetrievalText();
        this.applicability = content.applicability();
        this.repairStrategy = content.repairStrategy();
        this.verificationStrategy = content.verificationStrategy();
        this.antiPatterns = content.antiPatterns();
        this.languageName = content.languageName();
        this.frameworkName = content.frameworkName();
        this.problemType = content.problemType();
        this.dependencyRange = content.dependencyRange();
        this.updatedAt = Instant.now();
    }

    public void recordOutcome(boolean success) {
        if (success) successfulUses++;
        else failedUses++;
        long total = successfulUses + failedUses;
        qualityScore = total == 0 ? 0.5 : (double) successfulUses / total;
        updatedAt = Instant.now();
    }

    public void updateEmbedding(String embeddingJson, String model, String profile, int dimensions, long revision) {
        if (revision != this.revision) return;
        this.embeddingJson = embeddingJson;
        this.embeddingModel = model;
        this.embeddingProfile = profile;
        this.embeddingDimensions = dimensions;
        this.embeddingRevision = revision;
        this.updatedAt = Instant.now();
    }

    public MemoryContent content() {
        return new MemoryContent(title, summary, retrievalText, applicability, repairStrategy, verificationStrategy,
                antiPatterns, languageName, frameworkName, problemType, dependencyRange);
    }

    public String embeddingText() {
        return String.join("\n", title, retrievalText == null || retrievalText.isBlank() ? summary : retrievalText,
                applicability);
    }

    public UUID getId() { return id; }
    public MemoryScopeType getScopeType() { return scopeType; }
    public String getScopeKey() { return scopeKey; }
    public UUID getRepositoryId() { return repositoryId; }
    public String getTitle() { return title; }
    public String getSummary() { return summary; }
    public String getRetrievalText() { return retrievalText; }
    public String getApplicability() { return applicability; }
    public String getRepairStrategy() { return repairStrategy; }
    public String getVerificationStrategy() { return verificationStrategy; }
    public String getAntiPatterns() { return antiPatterns; }
    public String getLanguageName() { return languageName; }
    public String getFrameworkName() { return frameworkName; }
    public String getProblemType() { return problemType; }
    public String getDependencyRange() { return dependencyRange; }
    public String getFingerprint() { return fingerprint; }
    public MemoryStatus getStatus() { return status; }
    public double getQualityScore() { return qualityScore; }
    public long getSuccessfulUses() { return successfulUses; }
    public long getFailedUses() { return failedUses; }
    public long getVersion() { return revision; }
    public String getEmbeddingJson() { return embeddingJson; }
    public String getEmbeddingModel() { return embeddingModel; }
    public String getEmbeddingProfile() { return embeddingProfile; }
    public Integer getEmbeddingDimensions() { return embeddingDimensions; }
    public Long getEmbeddingRevision() { return embeddingRevision; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }
}
