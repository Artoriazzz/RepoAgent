ALTER TABLE repair_task
    ADD COLUMN submitted_by_user_id CHAR(36) COLLATE utf8mb4_unicode_ci NULL,
    ADD COLUMN workflow_status VARCHAR(32) NULL,
    ADD COLUMN current_pipeline_stage VARCHAR(64) NULL,
    ADD COLUMN stage_status VARCHAR(32) NULL,
    ADD COLUMN stage_sequence BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN lease_epoch BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN capacity_released_at TIMESTAMP(6) NULL,
    ADD COLUMN admitted_at TIMESTAMP(6) NULL;

UPDATE repair_task
SET workflow_status = CASE
        WHEN task_status = 'CANCELLED' THEN 'CANCELLED'
        WHEN task_status = 'FAILED' THEN 'FAILED'
        WHEN evaluation_status = 'RESOLVED' THEN 'SUCCEEDED'
        WHEN evaluation_status IN ('UNRESOLVED', 'HARNESS_ERROR') THEN 'FAILED'
        ELSE 'RUNNING'
    END,
    current_pipeline_stage = pipeline_stage,
    stage_status = CASE
        WHEN task_status IN ('ANALYZING', 'FIXING', 'VALIDATING') AND worker_id IS NOT NULL THEN 'RUNNING'
        WHEN task_status IN ('FAILED', 'CANCELLED') THEN 'FAILED'
        WHEN evaluation_status = 'RESOLVED' THEN 'SUCCEEDED'
        ELSE 'WAITING'
    END,
    admitted_at = created_at;

CREATE INDEX ix_repair_task_user_workflow ON repair_task(submitted_by_user_id, workflow_status);
CREATE UNIQUE INDEX uk_repair_task_user_idempotency ON repair_task(submitted_by_user_id, idempotency_key);
CREATE INDEX ix_repair_task_stage_waiting ON repair_task(workflow_status, current_pipeline_stage, stage_status, created_at);
CREATE INDEX ix_repair_task_expired_lease_v2 ON repair_task(workflow_status, stage_status, lease_expires_at);

CREATE TABLE repair_user_quota (
    user_id CHAR(36) PRIMARY KEY,
    max_active_workflows INTEGER NOT NULL DEFAULT 3,
    daily_task_limit INTEGER NOT NULL DEFAULT 20,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT fk_repair_user_quota_user FOREIGN KEY (user_id) REFERENCES app_user(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE repair_user_runtime (
    user_id CHAR(36) PRIMARY KEY,
    active_workflows INTEGER NOT NULL DEFAULT 0,
    row_version BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT fk_repair_user_runtime_user FOREIGN KEY (user_id) REFERENCES app_user(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE repair_daily_usage (
    id CHAR(36) PRIMARY KEY,
    user_id CHAR(36) NOT NULL,
    usage_date DATE NOT NULL,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT uk_repair_daily_user_date UNIQUE (user_id, usage_date),
    CONSTRAINT fk_repair_daily_usage_user FOREIGN KEY (user_id) REFERENCES app_user(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE repair_system_capacity (
    capacity_key VARCHAR(64) PRIMARY KEY,
    active_workflows INTEGER NOT NULL DEFAULT 0,
    row_version BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO repair_system_capacity(capacity_key, active_workflows, row_version, updated_at)
VALUES ('GLOBAL', 0, 0, CURRENT_TIMESTAMP(6));

CREATE TABLE repair_stage_execution (
    id CHAR(36) PRIMARY KEY,
    task_id CHAR(36) NOT NULL,
    stage_sequence BIGINT NOT NULL,
    pipeline_stage VARCHAR(64) NOT NULL,
    stage_status VARCHAR(32) NOT NULL,
    lease_epoch BIGINT NOT NULL DEFAULT 0,
    worker_id VARCHAR(255) NULL,
    started_at TIMESTAMP(6) NULL,
    finished_at TIMESTAMP(6) NULL,
    duration_ms BIGINT NULL,
    error_code VARCHAR(255) NULL,
    error_summary TEXT NULL,
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT uk_repair_stage_sequence UNIQUE (task_id, stage_sequence),
    CONSTRAINT fk_repair_stage_execution_task FOREIGN KEY (task_id) REFERENCES repair_task(id)
 ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE repair_outbox_event ADD COLUMN stage_sequence BIGINT NOT NULL DEFAULT 0;
