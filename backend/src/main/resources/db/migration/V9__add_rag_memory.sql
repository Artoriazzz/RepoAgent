CREATE TABLE memory_observation (
    id CHAR(36) PRIMARY KEY,
    task_id CHAR(36) NOT NULL,
    repository_id CHAR(36) NOT NULL,
    idempotency_key VARCHAR(191) NOT NULL,
    outcome VARCHAR(32) NOT NULL,
    problem_signature TEXT NOT NULL,
    patch_summary LONGTEXT,
    validation_summary LONGTEXT,
    evidence_json LONGTEXT NOT NULL,
    created_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT uk_memory_observation_idempotency UNIQUE (idempotency_key),
    CONSTRAINT fk_memory_observation_task FOREIGN KEY (task_id) REFERENCES repair_task(id),
    CONSTRAINT fk_memory_observation_repository FOREIGN KEY (repository_id) REFERENCES code_repository(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_memory_observation_task ON memory_observation(task_id, created_at);
CREATE INDEX ix_memory_observation_repository ON memory_observation(repository_id, created_at);

CREATE TABLE semantic_memory (
    id CHAR(36) PRIMARY KEY,
    scope_type VARCHAR(32) NOT NULL,
    scope_key VARCHAR(191) NOT NULL,
    repository_id CHAR(36),
    title VARCHAR(255) NOT NULL,
    summary TEXT NOT NULL,
    applicability LONGTEXT NOT NULL,
    repair_strategy LONGTEXT NOT NULL,
    verification_strategy LONGTEXT,
    anti_patterns LONGTEXT,
    language_name VARCHAR(64),
    framework_name VARCHAR(128),
    problem_type VARCHAR(128),
    dependency_range VARCHAR(128),
    fingerprint CHAR(64) NOT NULL,
    memory_status VARCHAR(32) NOT NULL,
    quality_score DECIMAL(5,4) NOT NULL DEFAULT 0.5000,
    successful_uses BIGINT NOT NULL DEFAULT 0,
    failed_uses BIGINT NOT NULL DEFAULT 0,
    content_revision BIGINT NOT NULL DEFAULT 1,
    row_version BIGINT NOT NULL DEFAULT 0,
    embedding_json LONGTEXT,
    embedding_model VARCHAR(191),
    embedding_revision BIGINT,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT uk_semantic_memory_scope_fingerprint UNIQUE (scope_key, fingerprint),
    CONSTRAINT fk_semantic_memory_repository FOREIGN KEY (repository_id) REFERENCES code_repository(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_semantic_memory_recall
    ON semantic_memory(memory_status, scope_key, language_name, framework_name, problem_type);

CREATE TABLE memory_revision (
    id CHAR(36) PRIMARY KEY,
    memory_id CHAR(36) NOT NULL,
    revision_number BIGINT NOT NULL,
    title VARCHAR(255) NOT NULL,
    summary TEXT NOT NULL,
    applicability LONGTEXT NOT NULL,
    repair_strategy LONGTEXT NOT NULL,
    verification_strategy LONGTEXT,
    anti_patterns LONGTEXT,
    language_name VARCHAR(64),
    framework_name VARCHAR(128),
    problem_type VARCHAR(128),
    dependency_range VARCHAR(128),
    source_observation_id CHAR(36) NOT NULL,
    change_reason TEXT NOT NULL,
    created_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT uk_memory_revision_number UNIQUE (memory_id, revision_number),
    CONSTRAINT fk_memory_revision_memory FOREIGN KEY (memory_id) REFERENCES semantic_memory(id),
    CONSTRAINT fk_memory_revision_observation FOREIGN KEY (source_observation_id) REFERENCES memory_observation(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE memory_proposal (
    id CHAR(36) PRIMARY KEY,
    memory_id CHAR(36),
    observation_id CHAR(36) NOT NULL,
    idempotency_key VARCHAR(191) NOT NULL,
    operation_type VARCHAR(32) NOT NULL,
    base_version BIGINT,
    proposed_payload_json LONGTEXT,
    proposal_status VARCHAR(32) NOT NULL,
    conflict_reason TEXT,
    applied_revision BIGINT,
    created_at TIMESTAMP(6) NOT NULL,
    processed_at TIMESTAMP(6),
    CONSTRAINT uk_memory_proposal_idempotency UNIQUE (idempotency_key),
    CONSTRAINT fk_memory_proposal_memory FOREIGN KEY (memory_id) REFERENCES semantic_memory(id),
    CONSTRAINT fk_memory_proposal_observation FOREIGN KEY (observation_id) REFERENCES memory_observation(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_memory_proposal_status ON memory_proposal(proposal_status, created_at);

CREATE TABLE memory_index_job (
    id CHAR(36) PRIMARY KEY,
    memory_id CHAR(36) NOT NULL,
    memory_revision BIGINT NOT NULL,
    job_status VARCHAR(32) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP(6) NOT NULL,
    claimed_at TIMESTAMP(6),
    last_error TEXT,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT uk_memory_index_revision UNIQUE (memory_id, memory_revision),
    CONSTRAINT fk_memory_index_memory FOREIGN KEY (memory_id) REFERENCES semantic_memory(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_memory_index_dispatch ON memory_index_job(job_status, next_attempt_at, created_at);

CREATE TABLE memory_recall (
    id CHAR(36) PRIMARY KEY,
    task_id CHAR(36) NOT NULL,
    memory_id CHAR(36) NOT NULL,
    memory_revision BIGINT NOT NULL,
    recall_score DECIMAL(6,5) NOT NULL,
    used_by_agent BOOLEAN,
    successful BOOLEAN,
    created_at TIMESTAMP(6) NOT NULL,
    feedback_at TIMESTAMP(6),
    CONSTRAINT uk_memory_recall_task_revision UNIQUE (task_id, memory_id, memory_revision),
    CONSTRAINT fk_memory_recall_task FOREIGN KEY (task_id) REFERENCES repair_task(id),
    CONSTRAINT fk_memory_recall_memory FOREIGN KEY (memory_id) REFERENCES semantic_memory(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_memory_recall_memory ON memory_recall(memory_id, created_at);
