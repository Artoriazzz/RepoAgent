package com.artoria.repoagentbackend.github;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.UUID;

@Repository
public class WebhookDeliveryStore {
    private final JdbcTemplate jdbcTemplate;

    public WebhookDeliveryStore(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public boolean recordIfNew(String deliveryId, String eventType, Instant receivedAt) {
        int inserted = jdbcTemplate.update("""
                INSERT IGNORE INTO webhook_delivery (id, delivery_id, event_type, delivery_status, received_at)
                VALUES (?, ?, ?, 'PROCESSED', ?)
                """, UUID.randomUUID().toString(), deliveryId, eventType, receivedAt);
        return inserted == 1;
    }
}
