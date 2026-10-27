ALTER TABLE repair_task
    ADD COLUMN evaluation_status VARCHAR(32) NOT NULL DEFAULT 'NOT_RUN',
    ADD COLUMN fail_to_pass_passed INTEGER,
    ADD COLUMN fail_to_pass_failed INTEGER,
    ADD COLUMN pass_to_pass_passed INTEGER,
    ADD COLUMN pass_to_pass_failed INTEGER,
    ADD COLUMN evaluation_summary TEXT,
    ADD COLUMN harness_log_path VARCHAR(4000),
    ADD COLUMN evaluated_at TIMESTAMP(6);
