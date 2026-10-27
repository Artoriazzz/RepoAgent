ALTER TABLE repair_task
    ADD COLUMN parent_task_id CHAR(36),
    ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN model_name VARCHAR(255),
    ADD COLUMN run_key VARCHAR(255),
    ADD COLUMN error_code VARCHAR(64),
    ADD COLUMN error_summary TEXT,
    ADD COLUMN retryable BOOLEAN,
    ADD COLUMN cancel_requested_at TIMESTAMP(6),
    ADD COLUMN worker_id VARCHAR(255),
    ADD COLUMN lease_token CHAR(36),
    ADD COLUMN lease_expires_at TIMESTAMP(6),
    ADD COLUMN last_heartbeat_at TIMESTAMP(6),
    ADD COLUMN duration_ms BIGINT,
    ADD COLUMN prompt_tokens BIGINT,
    ADD COLUMN completion_tokens BIGINT,
    ADD COLUMN total_tokens BIGINT,
    ADD CONSTRAINT fk_repair_task_parent FOREIGN KEY (parent_task_id) REFERENCES repair_task(id);

CREATE INDEX ix_repair_task_parent_task_id ON repair_task(parent_task_id);
CREATE INDEX ix_repair_task_lease_expires_at ON repair_task(lease_expires_at);
CREATE INDEX ix_repair_task_created_at ON repair_task(created_at);

CREATE TABLE repair_task_event (
    id CHAR(36) PRIMARY KEY,
    task_id CHAR(36) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    stage VARCHAR(64),
    message TEXT,
    metadata_json LONGTEXT,
    created_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT fk_repair_task_event_task FOREIGN KEY (task_id) REFERENCES repair_task(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_repair_task_event_task_created ON repair_task_event(task_id, created_at);

CREATE TABLE worker_node (
    worker_id VARCHAR(255) PRIMARY KEY,
    status VARCHAR(32) NOT NULL,
    active_task_id CHAR(36),
    metadata_json LONGTEXT,
    last_heartbeat_at TIMESTAMP(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_worker_node_last_heartbeat ON worker_node(last_heartbeat_at);
