ALTER TABLE repair_task
    ADD COLUMN deleted_at TIMESTAMP(6);

CREATE INDEX ix_repair_task_deleted_at ON repair_task(deleted_at);
