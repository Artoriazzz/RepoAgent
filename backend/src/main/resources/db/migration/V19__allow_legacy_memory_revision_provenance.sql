-- 历史 SWE-bench 经验没有当前系统中的 RepairTask/Observation；保留 revision，
-- 但将来源通过 change_reason 记录，而不是伪造一条运行时 observation。
ALTER TABLE memory_revision
    MODIFY COLUMN source_observation_id CHAR(36) NULL;
