ALTER TABLE repository_issue
    ADD COLUMN base_sha VARCHAR(128);

UPDATE repository_issue issue
JOIN (
    SELECT issue_id, MAX(base_sha) AS base_sha
    FROM repair_task
    GROUP BY issue_id
) existing_task ON existing_task.issue_id = issue.id
SET issue.base_sha = existing_task.base_sha
WHERE issue.base_sha IS NULL;

CREATE INDEX ix_repository_issue_repair_queue
    ON repository_issue(issue_state, repository_id, created_at);
