package com.artoria.repoagentbackend.identity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import org.hibernate.annotations.CreationTimestamp;
import org.hibernate.annotations.UpdateTimestamp;
import org.hibernate.annotations.UuidGenerator;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "app_user")
public class AppUser {

    @Id
    @UuidGenerator
    private UUID id;

    @Column(name = "github_user_id", nullable = false, unique = true)
    private long githubUserId;

    @Column(nullable = false, unique = true)
    private String login;

    @Column(name = "display_name", nullable = false)
    private String displayName;

    @CreationTimestamp
    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @UpdateTimestamp
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected AppUser() {
    }

    public AppUser(long githubUserId, String login, String displayName) {
        this.githubUserId = githubUserId;
        this.login = login;
        this.displayName = displayName;
    }

    public UUID getId() { return id; }
    public long getGithubUserId() { return githubUserId; }
    public String getLogin() { return login; }
    public String getDisplayName() { return displayName; }
}

