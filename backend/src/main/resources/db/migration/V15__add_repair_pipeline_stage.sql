ALTER TABLE repair_task
    ADD COLUMN pipeline_stage VARCHAR(64) NOT NULL DEFAULT 'COORDINATOR';

CREATE INDEX ix_repair_task_pipeline_stage
    ON repair_task(pipeline_stage, task_status, lease_expires_at);
