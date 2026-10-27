CREATE TABLE code_repository (
    id CHAR(36) PRIMARY KEY,
    github_repository_id BIGINT NOT NULL UNIQUE,
    owner_login VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    default_branch VARCHAR(255) NOT NULL,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE repository_issue (
    id CHAR(36) PRIMARY KEY,
    repository_id CHAR(36) NOT NULL,
    github_issue_number INTEGER NOT NULL,
    title VARCHAR(500) NOT NULL,
    body LONGTEXT NOT NULL,
    issue_state VARCHAR(32) NOT NULL,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT fk_repository_issue_repository FOREIGN KEY (repository_id) REFERENCES code_repository(id),
    CONSTRAINT uq_repository_issue_number UNIQUE (repository_id, github_issue_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE repair_task (
    id CHAR(36) PRIMARY KEY,
    issue_id CHAR(36) NOT NULL,
    base_sha VARCHAR(128) NOT NULL,
    branch_name VARCHAR(255) NOT NULL UNIQUE,
    task_status VARCHAR(32) NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT fk_repair_task_issue FOREIGN KEY (issue_id) REFERENCES repository_issue(id),
    CONSTRAINT uq_repair_task_idempotency UNIQUE (issue_id, idempotency_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_repository_issue_repository_id ON repository_issue(repository_id);
CREATE INDEX ix_repair_task_issue_id ON repair_task(issue_id);
CREATE INDEX ix_repair_task_status ON repair_task(task_status);

CREATE TABLE webhook_delivery (
    id CHAR(36) PRIMARY KEY,
    delivery_id VARCHAR(255) NOT NULL UNIQUE,
    event_type VARCHAR(100) NOT NULL,
    delivery_status VARCHAR(32) NOT NULL,
    received_at TIMESTAMP(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
