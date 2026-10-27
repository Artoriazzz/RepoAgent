package com.artoria.repoagentbackend.memory;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "memory_revision")
public class MemoryRevision {
    @Id
    private UUID id;
    @Column(name = "memory_id", nullable = false)
    private UUID memoryId;
    @Column(name = "revision_number", nullable = false)
    private long revisionNumber;
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
    /**
     * 正常运行时关联一次已验证的修复观察。历史迁移记录没有当前系统中的 task，
     * 因而允许为空，并通过 changeReason 保留其已验证来源。
     */
    @Column(name = "source_observation_id")
    private UUID sourceObservationId;
    @Column(name = "change_reason", nullable = false, columnDefinition = "TEXT")
    private String changeReason;
    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    protected MemoryRevision() {
    }

    public MemoryRevision(SemanticMemory memory, UUID sourceObservationId, String changeReason) {
        MemoryContent content = memory.content();
        this.id = UUID.randomUUID();
        this.memoryId = memory.getId();
        this.revisionNumber = memory.getVersion();
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
        this.sourceObservationId = sourceObservationId;
        this.changeReason = changeReason;
        this.createdAt = Instant.now();
    }

    public UUID getId() { return id; }
    public UUID getMemoryId() { return memoryId; }
    public long getRevisionNumber() { return revisionNumber; }
    public UUID getSourceObservationId() { return sourceObservationId; }
    public String getChangeReason() { return changeReason; }
    public Instant getCreatedAt() { return createdAt; }
}
