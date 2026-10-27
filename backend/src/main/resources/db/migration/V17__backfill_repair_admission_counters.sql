-- V16 introduced ownership for new submissions. Attribute historical tasks to the
-- repository owner where that relationship is available before deriving counters.
UPDATE repair_task task
JOIN repository_issue issue_record ON issue_record.id = task.issue_id
JOIN code_repository repository_record ON repository_record.id = issue_record.repository_id
SET task.submitted_by_user_id = repository_record.owner_user_id
WHERE task.submitted_by_user_id IS NULL
  AND repository_record.owner_user_id IS NOT NULL;

INSERT INTO repair_user_runtime(user_id, active_workflows, row_version, updated_at)
SELECT submitted_by_user_id, COUNT(*), 0, CURRENT_TIMESTAMP(6)
FROM repair_task
WHERE workflow_status = 'RUNNING'
  AND submitted_by_user_id IS NOT NULL
GROUP BY submitted_by_user_id
ON DUPLICATE KEY UPDATE active_workflows = VALUES(active_workflows), updated_at = VALUES(updated_at);

UPDATE repair_system_capacity
SET active_workflows = (
    SELECT COUNT(*) FROM repair_task WHERE workflow_status = 'RUNNING'
), updated_at = CURRENT_TIMESTAMP(6)
WHERE capacity_key = 'GLOBAL';
