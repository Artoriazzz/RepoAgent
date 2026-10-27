package com.artoria.repoagentbackend.repair;

import java.time.Instant;
import java.util.UUID;

public record RepairOutboxResponse(
        UUID id,
        UUID aggregateId,
        String eventType,
        RepairOutboxStatus status,
        int attempts,
        Instant nextAttemptAt,
        Instant publishedAt,
        String lastError,
        Instant createdAt
) {
    public static RepairOutboxResponse from(RepairOutboxEvent event) {
        return new RepairOutboxResponse(
                event.getId(), event.getAggregateId(), event.getEventType(), event.getStatus(),
                event.getAttempts(), event.getNextAttemptAt(), event.getPublishedAt(),
                event.getLastError(), event.getCreatedAt()
        );
    }
}

