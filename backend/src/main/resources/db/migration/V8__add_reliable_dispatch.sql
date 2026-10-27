CREATE TABLE repair_outbox_event (
    id CHAR(36) PRIMARY KEY,
    aggregate_id CHAR(36) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    payload_json LONGTEXT NOT NULL,
    outbox_status VARCHAR(32) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP(6) NOT NULL,
    published_at TIMESTAMP(6),
    last_error TEXT,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT uk_repair_outbox_aggregate_event UNIQUE (aggregate_id, event_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_repair_outbox_dispatch
    ON repair_outbox_event(outbox_status, next_attempt_at, created_at);

