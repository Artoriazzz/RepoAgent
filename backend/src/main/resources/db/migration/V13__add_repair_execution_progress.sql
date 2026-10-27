ALTER TABLE repair_task
    ADD COLUMN current_stage VARCHAR(64),
    ADD COLUMN current_message TEXT,
    ADD COLUMN correction_attempt INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN max_correction_attempts INTEGER NOT NULL DEFAULT 1;
