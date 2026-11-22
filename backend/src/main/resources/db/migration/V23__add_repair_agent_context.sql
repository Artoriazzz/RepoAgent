CREATE TABLE repair_agent_context (
    context_key VARCHAR(112) PRIMARY KEY,
    task_id CHAR(36) NOT NULL,
    stage_sequence BIGINT NOT NULL,
    pipeline_stage VARCHAR(64) NOT NULL,
    body LONGTEXT NOT NULL,
    CONSTRAINT uk_repair_agent_context_stage UNIQUE (task_id, stage_sequence),
    CONSTRAINT fk_repair_agent_context_task FOREIGN KEY (task_id) REFERENCES repair_task(id) ON DELETE CASCADE,
    INDEX ix_repair_agent_context_role (task_id, pipeline_stage, stage_sequence)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
