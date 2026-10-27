package com.artoria.repoagentbackend.repository;

import com.artoria.repoagentbackend.identity.AppUser;
import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.FetchType;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;
import jakarta.persistence.Table;
import org.hibernate.annotations.CreationTimestamp;
import org.hibernate.annotations.UpdateTimestamp;
import org.hibernate.annotations.UuidGenerator;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "code_repository")
public class CodeRepository {

    @Id
    @UuidGenerator
    private UUID id;

    @Column(name = "github_repository_id", unique = true)
    private Long githubRepositoryId;

    @Enumerated(EnumType.STRING)
    @Column(name = "repository_provider", nullable = false)
    private RepositoryProvider provider;

    @Column(name = "external_id")
    private String externalId;

    @Column(name = "mirror_key")
    private String mirrorKey;

    @Column(name = "owner_login", nullable = false)
    private String ownerLogin;

    @Column(nullable = false)
    private String name;

    @Column(name = "default_branch", nullable = false)
    private String defaultBranch;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "owner_user_id")
    private AppUser owner;

    @CreationTimestamp
    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @UpdateTimestamp
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected CodeRepository() {
    }

    public CodeRepository(long githubRepositoryId, String ownerLogin, String name, String defaultBranch, AppUser owner) {
        this.githubRepositoryId = githubRepositoryId;
        this.provider = RepositoryProvider.GITHUB;
        this.externalId = ownerLogin + "/" + name;
        this.ownerLogin = ownerLogin;
        this.name = name;
        this.defaultBranch = defaultBranch;
        this.owner = owner;
    }

    public static CodeRepository sweBench(String repositoryName, AppUser owner) {
        String[] parts = repositoryName.split("/", 2);
        if (parts.length != 2) {
            throw new IllegalArgumentException("SWE-bench repository must use owner/name format");
        }
        CodeRepository repository = new CodeRepository();
        repository.provider = RepositoryProvider.SWE_BENCH;
        repository.externalId = repositoryName;
        repository.mirrorKey = parts[0] + "__" + parts[1];
        repository.ownerLogin = parts[0];
        repository.name = parts[1];
        repository.defaultBranch = "main";
        repository.owner = owner;
        return repository;
    }

    public UUID getId() { return id; }
    public Long getGithubRepositoryId() { return githubRepositoryId; }
    public RepositoryProvider getProvider() { return provider; }
    public String getExternalId() { return externalId; }
    public String getMirrorKey() { return mirrorKey; }
    public String getOwnerLogin() { return ownerLogin; }
    public String getName() { return name; }
    public String getDefaultBranch() { return defaultBranch; }
    public AppUser getOwner() { return owner; }
}
