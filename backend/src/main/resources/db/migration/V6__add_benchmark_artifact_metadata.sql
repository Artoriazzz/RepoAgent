ALTER TABLE repository_issue
    ADD COLUMN benchmark_dataset_split VARCHAR(64);

ALTER TABLE repair_task
    ADD COLUMN patch_artifact_path VARCHAR(4000);
