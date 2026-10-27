ALTER TABLE worker_node
    ADD COLUMN worker_role VARCHAR(64),
    ADD COLUMN instance_id VARCHAR(128);
