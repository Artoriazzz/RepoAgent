ALTER TABLE memory_proposal
    ADD COLUMN scope_type VARCHAR(32) NULL,
    ADD COLUMN change_reason TEXT NULL,
    ADD COLUMN merge_batch_id CHAR(36) NULL;

CREATE INDEX ix_memory_proposal_merge_batch ON memory_proposal(merge_batch_id, proposal_status);
CREATE INDEX ix_memory_proposal_pending_update
    ON memory_proposal(proposal_status, operation_type, memory_id, base_version, created_at);

CREATE TABLE memory_merge_batch (
    id CHAR(36) PRIMARY KEY,
    memory_id CHAR(36) NULL,
    scope_type VARCHAR(32) NULL,
    repository_id CHAR(36) NULL,
    operation_type VARCHAR(32) NOT NULL,
    base_version BIGINT NULL,
    batch_status VARCHAR(32) NOT NULL,
    worker_id VARCHAR(255) NOT NULL,
    lease_token CHAR(36) NOT NULL,
    lease_expires_at TIMESTAMP(6) NOT NULL,
    proposal_count INTEGER NOT NULL,
    failure_reason TEXT NULL,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    completed_at TIMESTAMP(6) NULL,
    row_version BIGINT NOT NULL DEFAULT 0,
    CONSTRAINT fk_memory_merge_batch_memory
        FOREIGN KEY (memory_id) REFERENCES semantic_memory(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_memory_merge_batch_claim
    ON memory_merge_batch(batch_status, lease_expires_at, created_at);
CREATE INDEX ix_memory_merge_batch_memory_version
    ON memory_merge_batch(memory_id, base_version, batch_status);
CREATE INDEX ix_memory_merge_batch_create_scope
    ON memory_merge_batch(operation_type, scope_type, repository_id, batch_status);
