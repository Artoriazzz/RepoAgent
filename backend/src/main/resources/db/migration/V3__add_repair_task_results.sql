ALTER TABLE repair_task ADD COLUMN patch_summary TEXT;
ALTER TABLE repair_task ADD COLUMN validation_summary TEXT;
ALTER TABLE repair_task ADD COLUMN pr_url TEXT;
ALTER TABLE repair_task ADD COLUMN completed_at TIMESTAMP(6);
